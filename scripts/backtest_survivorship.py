"""
Survivorship-bias test for the momentum basket.

THE BIAS
--------
`FINDINGS_momentum.md` reports 30/5/7 beating buy-and-hold BTC across a full
cycle. But the 11-token universe (`config/universe.py`) consists entirely of
tokens that **survived to 2026**. The universe that actually existed in 2021
included LUNA, FTT, and others that went to zero.

Momentum rotation is *especially* exposed: it buys whatever ran hardest, which
is precisely the profile of tokens that later collapsed. Every headline number
in this project inherits this bias, and it has never been measured.

THE ESCAPE HATCH THIS FIXES
---------------------------
`scripts/backtest_momentum.py:run_strategy` does:

    rets = daily_ret.iloc[j][held].dropna()

If a held token's series ENDS mid-hold (delisting), the NaN is dropped and the
position silently **vanishes** rather than taking a loss. Adding dead tokens
without fixing this would understate the damage — the backtest would rotate
into a doomed token and then teleport out of it at delisting.

This script handles delisting explicitly via `--delist-loss`:
    1.0  = total loss (you could not exit; conservative, the default)
    0.0  = exit at last traded close (optimistic; assumes a fill)
Both are reported so the assumption is visible rather than buried.

WHAT THIS IS AND IS NOT
-----------------------
This is a **lower bound** on the bias, not a correction. A true fix needs
point-in-time universe membership: what was actually liquid and top-N on each
historical date. Adding a handful of known-dead tokens shows the DIRECTION and
rough MAGNITUDE of the effect. It does not produce an unbiased backtest.

PREREQ — backfill the dead tokens first (they are not in the universe):
    python -m collectors.price_backfill --symbol LUNCUSDT --months 72
    python -m collectors.price_backfill --symbol FTTUSDT  --months 72
    ...
Run with --check-availability first to see which actually return data.

USAGE
-----
    python -m scripts.backtest_survivorship --check-availability
    python -m scripts.backtest_survivorship
    python -m scripts.backtest_survivorship --delist-loss 0.0
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from itertools import product

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backtest_survivorship")

TRADING_DAYS_PER_YEAR = 365

# Candidate dead/collapsed tokens with Binance USDT pairs.
# NOT all will have data — run --check-availability. Binance renamed the
# original Terra LUNA to LUNC (Luna Classic) after the May-2022 collapse;
# "LUNAUSDT" now refers to Terra 2.0, a DIFFERENT asset. Using LUNAUSDT
# would silently splice two unrelated series — exactly the MATIC/POL trap
# config/universe.py already documents.
DEAD_CANDIDATES = [
    # Recovered from the public archive (collectors/binance_archive.py) —
    # NOT available from the live REST API, which only serves listed
    # symbols. These two are the defining collapses of the cycle and were
    # ABSENT from the first survivorship run.
    "LUNAUSDT_DEAD",  # original Terra LUNA, 2020-11 → 2022-05-31 (→ ~0)
    "USTUSDT_DEAD",   # original TerraUSD, through the May-2022 de-peg
    # Previously recovered via the live API (post-collapse relistings) —
    # retained, but note LUNC/USTC start AFTER their collapses and so
    # contribute little. The _DEAD series above are the real test.
    "LUNCUSDT",   # Terra Classic — relisted 2022-09, post-collapse
    "USTCUSDT",   # TerraUSD Classic — relisted 2023-03, post-de-peg
    "FTTUSDT",    # FTX token — Nov 2022
    "SRMUSDT",    # Serum — FTX-affiliated, collapsed
    "ANCUSDT",    # Anchor Protocol — Terra ecosystem
    "MIRUSDT",    # Mirror Protocol — Terra ecosystem
    "WAVESUSDT",  # Waves — ~99% drawdown 2022
]

LOOKBACKS_N = (14, 30, 60, 90)
HOLD_TOP_K = (1, 3, 5)
REBALANCE_H = (7, 14, 30)


def load_closes(symbols: list[str]) -> pd.DataFrame:
    """Daily last-close per symbol from price_snapshots."""
    from config.database import get_connection, get_cursor
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT DISTINCT ON (symbol, timestamp::date)
                   symbol, timestamp::date AS d, close
            FROM price_snapshots
            WHERE symbol = ANY(%s)
            ORDER BY symbol, timestamp::date, timestamp DESC
        """, (symbols,))
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["symbol", "d", "close"]) \
        if not isinstance(rows[0], dict) else pd.DataFrame(rows)
    df["d"] = pd.to_datetime(df["d"])
    return df.pivot(index="d", columns="symbol", values="close").sort_index()


def build_delist_map(closes: pd.DataFrame) -> dict:
    """Last date each symbol has a price. A symbol whose series ends
    before the panel ends was delisted (or stopped trading)."""
    panel_end = closes.index[-1]
    out = {}
    for sym in closes.columns:
        last = closes[sym].last_valid_index()
        if last is not None and last < panel_end:
            out[sym] = last
    return out


def run_strategy(closes: pd.DataFrame, n: int, k: int, h: int,
                 cost_rate: float, delist_loss: float,
                 delist_map: dict) -> dict:
    """Momentum rotation with EXPLICIT delisting handling.

    Differs from backtest_momentum.run_strategy in one critical way: when
    a held token's price series ends, the position is not silently
    dropped. It takes `delist_loss` (1.0 = total loss) on the delisting
    date, then leaves the basket.
    """
    daily_ret = closes.pct_change()
    dates = closes.index

    equity = [1.0]
    equity_dates = [dates[n]]
    held: list[str] = []
    i = n

    while i < len(dates) - 1:
        now_px, past_px = closes.iloc[i], closes.iloc[i - n]
        trailing = (now_px / past_px - 1.0).dropna()
        ranked = trailing.sort_values(ascending=False)
        eligible = ranked[ranked > 0]
        new_held = list(eligible.index[:k])

        turnover = (len(set(held).symmetric_difference(set(new_held)))
                    / (2 * max(k, 1)))
        held = new_held
        equity_now = equity[-1] * (1.0 - turnover * cost_rate)

        end = min(i + h, len(dates) - 1)
        for j in range(i + 1, end + 1):
            day = dates[j]
            if held:
                contribs = []
                still_held = []
                basket_size = len(held)
                for sym in held:
                    delist_date = delist_map.get(sym)
                    if delist_date is not None and day > delist_date:
                        # Series ended while we held it. Charge the loss
                        # ONCE and drop the name. Without this branch the
                        # NaN would be dropna()'d and the position would
                        # vanish for free — the survivorship escape.
                        contribs.append(-delist_loss)
                        continue
                    r = daily_ret.iloc[j].get(sym, np.nan)
                    if not pd.isna(r):
                        contribs.append(float(r))
                        still_held.append(sym)
                # Equal-weight across the ORIGINAL basket size, so a
                # wiped-out name dilutes the book rather than being
                # excluded from the average.
                day_ret = (sum(contribs) / basket_size) if contribs else 0.0
                held = still_held
            else:
                day_ret = 0.0
            equity_now *= (1.0 + day_ret)
            equity.append(equity_now)
            equity_dates.append(day)
        i = end

    curve = pd.Series(equity, index=pd.DatetimeIndex(equity_dates))
    curve = curve[~curve.index.duplicated(keep="last")]
    return _metrics_from_curve(curve)


def _metrics_from_curve(curve: pd.Series) -> dict:
    if len(curve) < 2:
        return {"final": float("nan"), "cagr": float("nan"),
                "vol": float("nan"), "ret_vol": float("nan"),
                "max_dd": float("nan")}
    total_days = (curve.index[-1] - curve.index[0]).days or 1
    years = total_days / 365.0
    final = float(curve.iloc[-1])
    cagr = final ** (1.0 / years) - 1.0 if final > 0 else -1.0
    daily = curve.pct_change().dropna()
    vol = float(daily.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
    ret_vol = cagr / vol if vol > 0 else float("nan")
    max_dd = float((curve / curve.cummax() - 1.0).min())
    return {"final": final, "cagr": cagr, "vol": vol,
            "ret_vol": ret_vol, "max_dd": max_dd}


def dead_holdings_report(closes: pd.DataFrame, n: int, k: int, h: int,
                         dead: list[str]) -> list:
    """Which dead tokens did momentum actually SELECT, and when?

    The grid answers 'how much damage'. This answers the question that
    makes the damage believable: did the strategy really rotate into
    doomed tokens before they died? If LUNA was never selected, the
    penalty comes from elsewhere and the story is wrong.
    """
    dates = closes.index
    events, held = [], []
    i = n
    while i < len(dates) - 1:
        trailing = (closes.iloc[i] / closes.iloc[i - n] - 1.0).dropna()
        ranked = trailing.sort_values(ascending=False)
        new_held = list(ranked[ranked > 0].index[:k])
        for sym in new_held:
            if sym in dead and sym not in held:
                fwd_end = min(i + h, len(dates) - 1)
                px_in = closes.iloc[i].get(sym)
                px_out = closes.iloc[fwd_end].get(sym)
                events.append({
                    "date": dates[i].date(),
                    "symbol": sym,
                    "entry_px": px_in,
                    "exit_px": px_out,
                    "ret_pct": (100.0 * (px_out / px_in - 1.0)
                                if px_in and px_out and px_in > 0
                                else float("nan")),
                })
        held = new_held
        i = min(i + h, len(dates) - 1)
    return events


def main() -> int:
    p = argparse.ArgumentParser(description="Survivorship-bias test")
    p.add_argument("--cost-bps", type=float, default=10.0)
    p.add_argument("--delist-loss", type=float, default=1.0,
                   help="Loss on delisting: 1.0 = total (default), "
                        "0.0 = exit at last close.")
    p.add_argument("--check-availability", action="store_true",
                   help="Report which dead tokens have data, then exit.")
    args = p.parse_args()

    from config.universe import universe
    survivors = list(universe())

    if args.check_availability:
        closes = load_closes(survivors + DEAD_CANDIDATES)
        print(f"\n{'symbol':<14}{'first':>12}{'last':>12}{'days':>8}")
        for sym in survivors + DEAD_CANDIDATES:
            if sym in closes.columns:
                s = closes[sym].dropna()
                tag = "" if sym in survivors else "  (dead-candidate)"
                print(f"{sym:<14}{str(s.index[0].date()):>12}"
                      f"{str(s.index[-1].date()):>12}{len(s):>8}{tag}")
            else:
                print(f"{sym:<14}{'— NO DATA — backfill it first':>32}")
        print("\nBackfill missing ones with:")
        print("  python -m collectors.price_backfill --symbol <SYM> --months 72")
        return 0

    cost_rate = args.cost_bps / 10_000.0

    surv_only = load_closes(survivors)
    extended = load_closes(survivors + DEAD_CANDIDATES)
    dead_present = [s for s in DEAD_CANDIDATES if s in extended.columns]

    if not dead_present:
        logger.error("No dead-token data found. Run --check-availability "
                     "and backfill first — this test is meaningless "
                     "without them.")
        return 1

    delist_map = build_delist_map(extended)
    logger.info("Survivors: %d | dead tokens present: %d %s",
                len(survivors), len(dead_present), dead_present)
    logger.info("Delisted (series ends early): %s",
                {k: str(v.date()) for k, v in delist_map.items()})

    print(f"\n{'SURVIVORSHIP TEST':-^100}")
    print(f"delist_loss = {args.delist_loss:.0%} "
          f"({'total loss' if args.delist_loss >= 1 else 'exit at last close'})")
    print(f"{'config':<14}{'surv final':>12}{'ext final':>12}{'Δ%':>9}"
          f"{'surv r/v':>10}{'ext r/v':>10}{'surv DD':>10}{'ext DD':>10}")

    degraded = 0
    rows = []
    for n, k, h in product(LOOKBACKS_N, HOLD_TOP_K, REBALANCE_H):
        a = run_strategy(surv_only, n, k, h, cost_rate, args.delist_loss, {})
        b = run_strategy(extended, n, k, h, cost_rate, args.delist_loss,
                         delist_map)
        pct = (b["final"] / a["final"] - 1.0) * 100 if a["final"] else float("nan")
        if b["final"] < a["final"]:
            degraded += 1
        rows.append((n, k, h, a, b))
        print(f"{f'{n}/{k}/{h}':<14}{a['final']:>12.2f}{b['final']:>12.2f}"
              f"{pct:>+9.1f}{a['ret_vol']:>10.2f}{b['ret_vol']:>10.2f}"
              f"{a['max_dd']:>10.1%}{b['max_dd']:>10.1%}")

    print(f"\n{degraded}/{len(rows)} configs degraded when dead tokens "
          f"are included.")
    hero_a = [r for r in rows if (r[0], r[1], r[2]) == (30, 5, 7)]
    if hero_a:
        _, _, _, a, b = hero_a[0]
        print(f"Headline 30/5/7: {a['final']:.2f}x → {b['final']:.2f}x "
              f"({(b['final']/a['final']-1)*100:+.1f}%), "
              f"ret/vol {a['ret_vol']:.2f} → {b['ret_vol']:.2f}")

    # Did momentum actually BUY the doomed tokens? Without this the
    # damage figure is unexplained.
    print(f"\n{'DEAD-TOKEN SELECTIONS (30/5/7)':-^70}")
    events = dead_holdings_report(extended, 30, 5, 7, dead_present)
    if not events:
        print("  none selected — the penalty comes from elsewhere; "
              "investigate before believing the headline number.")
    else:
        print(f"{'entry date':<14}{'symbol':<18}{'entry':>12}{'exit':>12}"
              f"{'hold ret':>11}")
        for e in events:
            ep = f"{e['entry_px']:.4f}" if e["entry_px"] else "—"
            xp = f"{e['exit_px']:.4f}" if e["exit_px"] else "—"
            rp = f"{e['ret_pct']:+.1f}%" if e["ret_pct"] == e["ret_pct"] else "—"
            print(f"{str(e['date']):<14}{e['symbol']:<18}{ep:>12}{xp:>12}{rp:>11}")
        print(f"  {len(events)} entries into doomed tokens.")

    print("\nThis is a LOWER BOUND on the bias. A true correction needs "
          "point-in-time\nuniverse membership, not a handful of known-dead "
          "names added retroactively.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
