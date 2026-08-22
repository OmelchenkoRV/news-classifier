"""
Tradeable token universe.

Single source of truth for which crypto assets participate in
cross-sectional momentum ranking and rotation. Defining it here (rather
than scattering symbol lists across the backfill, the momentum ranker,
and any strategy module) means the universe is changed in exactly one
place and every consumer stays consistent.

Selection criteria (deliberate, and worth stating because they bias
results):
  - LIQUID Binance USDT pairs only. Momentum on illiquid microcaps
    backtests beautifully and is untradeable — slippage eats the edge.
    We restrict to majors/large-caps that can actually be rotated into
    at size.
  - DIVERSE enough to have a real cross-section. Momentum ranking needs
    a spread of assets to rank against each other; two assets isn't a
    cross-section, it's a coin flip.

KNOWN BIAS — survivorship:
  This is a FIXED, present-day list. Tokens that existed historically
  but died (LUNA, FTT, etc.) are absent, so any momentum backtest over
  history is implicitly conditioned on "survived to today" and will be
  OPTIMISTIC relative to what a live strategy would have experienced.
  A point-in-time universe (the constituents as they were at each date)
  would fix this but is hard to source cleanly. Until then: treat
  momentum backtest results as an upper bound, not an expectation. This
  is the same honest-measurement discipline the news-classifier null
  taught — don't let the methodology flatter itself.

Symbols are Binance USDT-pair tickers, matching price_snapshots.symbol.
"""

from __future__ import annotations


# The rotation universe. 11 liquid majors. BTC/ETH are the existing
# pair; the rest are large-caps with multi-year Binance history.
#
# NOTE on exclusions:
#   MATIC — deliberately excluded. Binance delisted all MATIC spot
#   pairs on 2024-09-10 when Polygon rebranded MATIC→POL (1:1 swap).
#   A naive MATICUSDT backfill gets history up to Sep 2024 then stops;
#   POLUSDT only starts Sep 2024. Including either alone creates a seam
#   that breaks momentum ranking mid-backtest (a token that "vanishes"
#   or "appears" partway through). Stitching MATIC→POL at the swap is
#   possible but adds a special-case join we don't need — 11 liquid
#   names is ample cross-section. Revisit only if a stitched series is
#   genuinely warranted.
TRADEABLE_UNIVERSE: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "LTCUSDT",
    "ATOMUSDT",
)


def universe() -> tuple[str, ...]:
    """The tradeable token universe for momentum/rotation."""
    return TRADEABLE_UNIVERSE
