"""
Asset routing: which assets is a given headline category exposed to?

The Phase 6 calibration result was a clean null: news headlines did not
predict BTC/ETH excess returns at the category level, robustly across a
regime flip. The likely reason is structural, not a modelling failure —
most headline categories simply aren't catalysts for crypto. A
"geopolitical: Iran closes Hormuz" headline is a major oil catalyst and
a non-event for Bitcoin. We were measuring headlines against assets that
don't react to them.

This module is the explicit, hand-built exposure map: each headline
category routes to the asset(s) whose price is actually sensitive to
that class of catalyst. The outcome scheduler consults this instead of
blindly scheduling every tracked asset for every trigger, which:

  - concentrates sample size on (category, asset) pairs that can
    plausibly carry signal, instead of diluting it across irrelevant
    pairs (the geopolitical-vs-BTC cells that dragged the null);
  - makes the hypothesis interpretable and falsifiable per pair
    ("does geopolitical news predict oil excess return?").

A learned/discovery layer (track everything, let calibration surface
which pairs show signal) is planned for later. This explicit table
encodes the domain priors we already hold and is the honest starting
point — if oil doesn't react to geopolitical news in the data, no
amount of discovery will save the thesis.

Categories come from training/train_classifier_v2.py CATEGORY_LABELS.
Assets are price_snapshots symbols (BTCUSDT, ETHUSDT, and now WTI).
"""

from __future__ import annotations


# Canonical asset symbols, matching price_snapshots.symbol values.
BTC = "BTCUSDT"
ETH = "ETHUSDT"
WTI = "WTI"          # West Texas Intermediate crude (yfinance CL=F)
GOLD = "GOLD"        # Gold front-month future (yfinance GC=F)

# Every asset the system can currently resolve outcomes for. Used as a
# sanity check that routing targets actually have a price source.
KNOWN_ASSETS: frozenset[str] = frozenset({BTC, ETH, WTI, GOLD})


# ── The routing table ────────────────────────────────────────────────
# category -> tuple of exposed assets.
#
# Reasoning per category:
#   geopolitical    Wars, sanctions, Hormuz, OPEC, Iran/Israel. The
#                   archetypal oil catalyst. Also a classic safe-haven
#                   catalyst for gold — fear bids gold up. Crypto is
#                   largely insensitive (the null we measured), so it's
#                   excluded here; geopolitical routes to oil + gold.
#                   NOTE: we do NOT assume gold moves antiphase to oil.
#                   They can spike together on a supply-shock-plus-fear
#                   event or diverge on a pure risk-off flush. The
#                   forecaster measures each asset's excess return
#                   independently, so whatever the real geopolitical
#                   gold/oil relationship is, calibration reveals it via
#                   the sign of each prior — we don't encode a belief.
#   macro           Fed rates, inflation prints, dollar strength,
#                   recession signals. Genuinely cross-asset: moves
#                   oil (demand expectations), crypto (liquidity / risk
#                   appetite), AND gold (real rates, dollar, inflation
#                   hedge). Routes to all four.
#   regulatory      SEC actions, ETF approvals, exchange crackdowns,
#                   stablecoin rules. Crypto-native. BTC/ETH only.
#   exchange_event  Hacks, outages, listings, delistings. Crypto-native.
#   whale_flow      Large on-chain moves, exchange in/outflows.
#                   Crypto-native.
#   protocol        Upgrades, forks, staking changes. Crypto-native.
#
# Reactive / non-event categories route to nothing — they never
# schedule outcomes. They're already gated upstream as REACTIVE in the
# classifier, but listing them here as empty makes the intent explicit
# and means an accidental upstream change can't silently start
# scheduling outcomes for opinion pieces.
ROUTING: dict[str, tuple[str, ...]] = {
    "geopolitical":    (WTI, GOLD),
    "macro":           (BTC, ETH, WTI, GOLD),
    "regulatory":      (BTC, ETH),
    "exchange_event":  (BTC, ETH),
    "whale_flow":      (BTC, ETH),
    "protocol":        (BTC, ETH),
    # reactive / non-catalyst — never schedule outcomes:
    "technical_analysis": (),
    "price_commentary":   (),
    "opinion":            (),
    "promotion":          (),
    "uncategorized":      (),
}


def assets_for_category(category: str) -> tuple[str, ...]:
    """Return the assets exposed to a given headline category.

    Unknown categories return an empty tuple (fail safe: schedule
    nothing rather than guess). This means a newly-added classifier
    category won't start generating outcomes until it's explicitly
    routed here — a deliberate gate, not an oversight.
    """
    return ROUTING.get(category, ())


def all_routed_assets() -> frozenset[str]:
    """Every asset that appears as a routing target. Useful for the
    price backfill to know which symbols it must keep fresh."""
    out: set[str] = set()
    for assets in ROUTING.values():
        out.update(assets)
    return frozenset(out)
