"""
STRATEGY LOGIC ADD-ON for your existing KalshiDemoBot script (82d7a944-attachment.txt).

Paste this class below your existing KalshiDemoBot class, then use
ComboKStrategy(bot) instead of calling bot methods directly for trading.

WHAT THIS ADDS (the pieces your script didn't have):
  1. Combo market discovery — attempts to find the BTC-up & ETH-up combo
     via /multivariate_event_collections (UNVERIFIED against your real
     demo account yet — this is the part we never got to test. Run
     discover_combo_collections() first and tell me what comes back
     before trusting anything downstream of it).
  2. Order book depth fetch for both legs.
  3. Vectorized (pandas) price-walking: buys matched pairs (1 combo +
     1 single per unit) level-by-level, tracking the running combined
     average cost, and STOPS the instant the next unit would push the
     combined average over $0.85.
  4. No standalone cap on the combo leg alone — only the combined $0.85
     total is enforced, per your last instruction.
  5. Returns a decision object: how many matched pairs it CAN fill
     within the cap, and their blended cost — before placing any real
     orders. You inspect this before executing.

This is intentionally read-only / dry-run by default. It computes what
the strategy WOULD do. Actual order placement is a separate, explicit
step (execute_matched_pairs) so nothing fires by accident.
"""

import pandas as pd


class ComboKStrategy:
    def __init__(self, bot):
        self.bot = bot  # an instance of your existing KalshiDemoBot

    # ------------------------------------------------------------------
    # 1. COMBO DISCOVERY — UNVERIFIED, run this first and report back
    # ------------------------------------------------------------------
    def discover_combo_collections(self):
        """
        Attempts to list multivariate (combo) event collections.
        This endpoint/response shape has NOT been confirmed against a
        real account yet. Run this and paste the raw output back before
        relying on anything else in this class that touches combos.
        """
        return self.bot.get("/trade-api/v2/multivariate_event_collections")

    def discover_combo_events(self, collection_ticker=None, series_ticker=None):
        """
        Lists multivariate events, optionally filtered by collection or series.
        Also unverified — same caveat as above.
        """
        params = {}
        if collection_ticker:
            params["collection_ticker"] = collection_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        return self.bot.get("/trade-api/v2/events/multivariate", params=params)

    # ------------------------------------------------------------------
    # 2. ORDER BOOK DEPTH
    # ------------------------------------------------------------------
    def get_orderbook_levels(self, market_ticker, side="yes"):
        """
        Fetches the live order book for one market and returns it as a
        DataFrame of [price, size], sorted best-price-first (ascending
        for a buy, since we're buying the 'yes' or 'no' side as a taker
        against resting bids on the other side).

        NOTE: Kalshi's orderbook only returns BIDS for yes/no (no asks) —
        see the docs excerpt from earlier. A "yes bid at 7c" = "no ask at
        93c". So to BUY yes, we look at resting NO bids (their bid is our
        effective ask), and vice versa. This function assumes you want to
        buy `side`, and reads the OPPOSITE side's bids as your fill levels.
        """
        book = self.bot.get(f"/trade-api/v2/markets/{market_ticker}/orderbook")
        ob = book.get("orderbook", book.get("orderbook_fp", {}))

        opposite = "no" if side == "yes" else "yes"
        levels = ob.get(f"{opposite}_dollars", ob.get(opposite, [])) or []

        if not levels:
            return pd.DataFrame(columns=["price", "size"])

        df = pd.DataFrame(levels, columns=["opp_price", "size"])
        # A resting NO bid at price X means you can BUY YES at (1.00 - X)
        df["price"] = (1.0 - df["opp_price"].astype(float)).round(4)
        df = df.sort_values("price").reset_index(drop=True)
        return df[["price", "size"]]

    # ------------------------------------------------------------------
    # 3. VECTORIZED MATCHED-PAIR PRICE WALK
    # ------------------------------------------------------------------
    def plan_matched_pairs(self, combo_levels: pd.DataFrame, single_levels: pd.DataFrame,
                            target_size: int, combined_cap: float = 0.85):
        """
        combo_levels / single_levels: DataFrames with columns [price, size],
        best price first (output of get_orderbook_levels).

        Buys 1 combo unit + 1 single unit per matched pair, walking each
        leg's own book independently, but checking the COMBINED running
        average cost after every matched pair. Stops the moment adding
        the next pair would push combined average > combined_cap.

        Returns a dict: {
            'filled_size': int,
            'combo_avg_price': float,
            'single_avg_price': float,
            'combined_avg_price': float,
            'combo_fills': DataFrame,
            'single_fills': DataFrame,
        }
        """
        # Expand each leg's book into a per-unit price ladder, capped at target_size
        def expand_ladder(levels, n):
            if levels.empty:
                return pd.Series(dtype=float)
            reps = levels["size"].astype(int).clip(upper=n)
            ladder = levels["price"].repeat(reps).reset_index(drop=True)
            return ladder.iloc[:n]

        combo_ladder = expand_ladder(combo_levels, target_size)
        single_ladder = expand_ladder(single_levels, target_size)

        # Matched pairs can only go as deep as the SHORTER available ladder
        max_matchable = min(len(combo_ladder), len(single_ladder))
        if max_matchable == 0:
            return {
                "filled_size": 0, "combo_avg_price": None, "single_avg_price": None,
                "combined_avg_price": None,
                "combo_fills": pd.DataFrame(), "single_fills": pd.DataFrame(),
            }

        combo_ladder = combo_ladder.iloc[:max_matchable].reset_index(drop=True)
        single_ladder = single_ladder.iloc[:max_matchable].reset_index(drop=True)

        # Vectorized cumulative average cost per matched-pair count (1..N)
        pair_cost = combo_ladder + single_ladder  # cost of the Nth pair
        cum_avg = pair_cost.cumsum() / (pair_cost.index + 1)

        # Largest N where cumulative combined average stays within cap
        eligible = cum_avg[cum_avg <= combined_cap]
        filled_size = int(eligible.index.max()) + 1 if len(eligible) > 0 else 0

        if filled_size == 0:
            return {
                "filled_size": 0, "combo_avg_price": None, "single_avg_price": None,
                "combined_avg_price": None,
                "combo_fills": pd.DataFrame(), "single_fills": pd.DataFrame(),
            }

        combo_fills = combo_ladder.iloc[:filled_size]
        single_fills = single_ladder.iloc[:filled_size]

        return {
            "filled_size": filled_size,
            "combo_avg_price": round(combo_fills.mean(), 4),
            "single_avg_price": round(single_fills.mean(), 4),
            "combined_avg_price": round(cum_avg.iloc[filled_size - 1], 4),
            "combo_fills": combo_fills,
            "single_fills": single_fills,
        }

    # ------------------------------------------------------------------
    # 4. FULL DRY-RUN PLAN (books both legs, walks prices, no orders sent)
    # ------------------------------------------------------------------
    def dry_run(self, combo_ticker, single_ticker, target_size, combined_cap=0.85):
        combo_levels = self.get_orderbook_levels(combo_ticker, side="yes")
        single_levels = self.get_orderbook_levels(single_ticker, side="yes")

        plan = self.plan_matched_pairs(combo_levels, single_levels, target_size, combined_cap)

        print("=" * 60)
        print(f"DRY RUN — target size {target_size}, combined cap ${combined_cap}")
        print("=" * 60)
        print(f"Combo book depth available: {combo_levels['size'].sum() if not combo_levels.empty else 0}")
        print(f"Single book depth available: {single_levels['size'].sum() if not single_levels.empty else 0}")
        print(f"\nFilled matched-pair size: {plan['filled_size']}")
        if plan["filled_size"] > 0:
            print(f"Combo avg price:    ${plan['combo_avg_price']}")
            print(f"Single avg price:   ${plan['single_avg_price']}")
            print(f"Combined avg price: ${plan['combined_avg_price']}  (cap ${combined_cap})")
        else:
            print("No fillable size within cap — trade would be REJECTED, no orders placed.")

        return plan

    # ------------------------------------------------------------------
    # 5. EXECUTE — only call this explicitly, after reviewing dry_run()
    # ------------------------------------------------------------------
    def execute_matched_pairs(self, combo_ticker, single_ticker, plan, dry_run_only=True):
        """
        Places the actual orders for a plan produced by dry_run()/plan_matched_pairs().
        dry_run_only=True (default) just prints what WOULD be sent — you must
        pass dry_run_only=False explicitly to actually call bot.place_order_v2().
        """
        if plan["filled_size"] == 0:
            print("Nothing to execute — plan had zero fillable size.")
            return None

        size = plan["filled_size"]
        combo_price = plan["combo_avg_price"]
        single_price = plan["single_avg_price"]

        print(f"{'[DRY RUN] ' if dry_run_only else ''}Would place:")
        print(f"  BUY {size}x {combo_ticker} @ ~${combo_price} (combo leg)")
        print(f"  BUY {size}x {single_ticker} @ ~${single_price} (single leg)")

        if dry_run_only:
            return {"status": "dry_run_only", "plan": plan}

        combo_order = self.bot.place_order_v2(combo_ticker, size, f"{combo_price:.4f}")
        single_order = self.bot.place_order_v2(single_ticker, size, f"{single_price:.4f}")
        return {"combo_order": combo_order, "single_order": single_order}


# ------------------------------------------------------------------
# EXAMPLE USAGE (append below your existing __main__ block, or run separately)
# ------------------------------------------------------------------
"""
strategy = ComboKStrategy(bot)  # reuse the `bot` you already authenticated

# STEP 1 — run this FIRST, alone, and report back the raw output.
# We have not confirmed this endpoint/response shape works on your account.
collections = strategy.discover_combo_collections()
print(json.dumps(collections, indent=2))

# STEP 2 — once we know the real combo ticker format, dry-run the plan:
# plan = strategy.dry_run(
#     combo_ticker="<real combo ticker once discovered>",
#     single_ticker="KXBTCD-26SEP2917-T92249.99",  # example from your successful run
#     target_size=1000,
#     combined_cap=0.85,
# )

# STEP 3 — only after reviewing the dry run, execute for real:
# strategy.execute_matched_pairs(combo_ticker, single_ticker, plan, dry_run_only=False)
"""
