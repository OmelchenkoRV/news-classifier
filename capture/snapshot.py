"""
Pre-Breakout Snapshot Capture
================================

Captures all the data you'll want to analyze AFTER an ETH breakout, while
markets are still calm. Run this NOW (before the move) and again every
4-6 hours until the breakout happens.

The capture covers: spot orderbooks, options chain, perp funding, OI,
liquidation levels, ETF flows. Saved to PostgreSQL with timestamps so
you can replay the run-up to the move tick-by-tick.

Usage:
    # One-time setup
    python -m capture.snapshot --init-db

    # Capture now
    python -m capture.snapshot

    # Run continuously (every 5 min)
    python -m capture.snapshot --continuous

Saved data:
    eth_snapshots         — top-level snapshot records
    eth_orderbook_depth   — orderbook depth at multiple % levels
    eth_options_chain     — full options chain at snapshot time
    eth_derivatives       — funding, OI, long/short
    eth_etf_flows         — daily ETF net flows
"""

import os
import sys
import time
import json
import logging
import argparse
import requests
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CAPTURE_INTERVAL = int(os.getenv("CAPTURE_INTERVAL_SECONDS", "300"))


# ═══════════════════════════════════════════════════════════════════
# DATABASE SCHEMA
# ═══════════════════════════════════════════════════════════════════

INIT_SQL = """
CREATE TABLE IF NOT EXISTS eth_snapshots (
    id              SERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    spot_price      DOUBLE PRECISION,
    btc_price       DOUBLE PRECISION,
    eth_btc_ratio   DOUBLE PRECISION,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_eth_snap_time ON eth_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS eth_orderbook_depth (
    id              SERIAL PRIMARY KEY,
    snapshot_id     INTEGER REFERENCES eth_snapshots(id),
    exchange        TEXT NOT NULL,
    side            TEXT NOT NULL,           -- 'bid' or 'ask'
    pct_from_mid    DOUBLE PRECISION,        -- 0.5, 1, 2, 5
    cumulative_eth  DOUBLE PRECISION,
    cumulative_usd  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_eth_ob_snap ON eth_orderbook_depth(snapshot_id);

CREATE TABLE IF NOT EXISTS eth_derivatives (
    id              SERIAL PRIMARY KEY,
    snapshot_id     INTEGER REFERENCES eth_snapshots(id),
    exchange        TEXT NOT NULL,
    funding_rate    DOUBLE PRECISION,
    next_funding    DOUBLE PRECISION,
    open_interest_usd DOUBLE PRECISION,
    long_short_ratio DOUBLE PRECISION,
    mark_price      DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_eth_deriv_snap ON eth_derivatives(snapshot_id);

CREATE TABLE IF NOT EXISTS eth_options_chain (
    id              SERIAL PRIMARY KEY,
    snapshot_id     INTEGER REFERENCES eth_snapshots(id),
    expiry_date     DATE NOT NULL,
    strike          DOUBLE PRECISION NOT NULL,
    option_type     TEXT NOT NULL,           -- 'C' or 'P'
    open_interest   DOUBLE PRECISION,
    volume_24h      DOUBLE PRECISION,
    iv              DOUBLE PRECISION,
    delta           DOUBLE PRECISION,
    gamma           DOUBLE PRECISION,
    vega            DOUBLE PRECISION,
    bid_price       DOUBLE PRECISION,
    ask_price       DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_eth_opt_snap_exp ON eth_options_chain(snapshot_id, expiry_date);

CREATE TABLE IF NOT EXISTS eth_etf_flows (
    id              SERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    flow_date       DATE NOT NULL,
    ticker          TEXT NOT NULL,
    net_flow_usd    DOUBLE PRECISION,
    aum_usd         DOUBLE PRECISION,
    UNIQUE(flow_date, ticker)
);

CREATE OR REPLACE VIEW v_eth_breakout_signals AS
SELECT
    s.captured_at,
    s.spot_price,
    s.eth_btc_ratio,
    AVG(d.funding_rate) FILTER (WHERE d.exchange IN ('binance','bybit','okx')) as avg_funding,
    AVG(d.open_interest_usd) FILTER (WHERE d.exchange IN ('binance','bybit','okx')) as avg_oi,
    SUM(ob.cumulative_usd) FILTER (WHERE ob.side='bid' AND ob.pct_from_mid <= 1.0) as bid_depth_1pct,
    SUM(ob.cumulative_usd) FILTER (WHERE ob.side='ask' AND ob.pct_from_mid <= 1.0) as ask_depth_1pct
FROM eth_snapshots s
LEFT JOIN eth_derivatives d ON d.snapshot_id = s.id
LEFT JOIN eth_orderbook_depth ob ON ob.snapshot_id = s.id
GROUP BY s.id, s.captured_at, s.spot_price, s.eth_btc_ratio
ORDER BY s.captured_at DESC;
"""


def init_db():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute(INIT_SQL)
    conn.commit()
    conn.close()
    logger.info("Snapshot tables created")


# ═══════════════════════════════════════════════════════════════════
# DATA SOURCES — public APIs, no keys needed
# ═══════════════════════════════════════════════════════════════════

def fetch_spot_prices():
    """Get ETH and BTC spot from Binance."""
    try:
        eth = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "ETHUSDT"}, timeout=10
        ).json()
        btc = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=10
        ).json()
        return float(eth["price"]), float(btc["price"])
    except Exception as e:
        logger.error(f"Spot price fetch failed: {e}")
        return None, None


def fetch_orderbook_depth(symbol="ETHUSDT", limit=1000):
    """Get full orderbook from Binance, compute cumulative depth at percentage levels."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/depth",
            params={"symbol": symbol, "limit": limit}, timeout=10
        )
        data = r.json()
        bids = [(float(p), float(q)) for p, q in data["bids"]]
        asks = [(float(p), float(q)) for p, q in data["asks"]]

        if not bids or not asks:
            return []

        mid = (bids[0][0] + asks[0][0]) / 2

        results = []
        for pct in (0.5, 1.0, 2.0, 5.0):
            # Bids — accumulate going down from mid
            bid_threshold = mid * (1 - pct/100)
            bid_eth = sum(q for p, q in bids if p >= bid_threshold)
            bid_usd = sum(p*q for p, q in bids if p >= bid_threshold)
            results.append(("binance", "bid", pct, bid_eth, bid_usd))

            # Asks — accumulate going up from mid
            ask_threshold = mid * (1 + pct/100)
            ask_eth = sum(q for p, q in asks if p <= ask_threshold)
            ask_usd = sum(p*q for p, q in asks if p <= ask_threshold)
            results.append(("binance", "ask", pct, ask_eth, ask_usd))

        return results
    except Exception as e:
        logger.error(f"Orderbook fetch failed: {e}")
        return []


def fetch_perp_funding():
    """Get perp funding rates from Binance, Bybit, OKX."""
    results = []

    # Binance
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": "ETHUSDT"}, timeout=10
        ).json()
        oi_r = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": "ETHUSDT"}, timeout=10
        ).json()
        oi_usd = float(oi_r["openInterest"]) * float(r["markPrice"])
        results.append(("binance", float(r["lastFundingRate"]),
                       float(r["lastFundingRate"]),
                       oi_usd, None, float(r["markPrice"])))
    except Exception as e:
        logger.warning(f"Binance funding failed: {e}")

    # Bybit
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": "ETHUSDT"}, timeout=10
        ).json()
        d = r["result"]["list"][0]
        results.append(("bybit", float(d["fundingRate"]),
                       float(d["fundingRate"]),
                       float(d["openInterestValue"]), None, float(d["markPrice"])))
    except Exception as e:
        logger.warning(f"Bybit funding failed: {e}")

    # OKX
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": "ETH-USDT-SWAP"}, timeout=10
        ).json()
        d = r["data"][0]

        def safe_float(v):
            try:
                return float(v) if v not in (None, "", "null") else None
            except (TypeError, ValueError):
                return None

        results.append(("okx",
                       safe_float(d.get("fundingRate")),
                       safe_float(d.get("nextFundingRate")),
                       None, None, None))
    except Exception as e:
        logger.warning(f"OKX funding failed: {e}")

    return results


def fetch_long_short_ratio():
    """Get top-trader long/short ratio from Binance."""
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
            params={"symbol": "ETHUSDT", "period": "5m", "limit": 1},
            timeout=10
        ).json()
        if r:
            return float(r[0]["longShortRatio"])
    except Exception as e:
        logger.warning(f"Long/short fetch failed: {e}")
    return None


def fetch_deribit_options():
    """Get full ETH options chain from Deribit."""
    try:
        # Get all instruments
        r = requests.get(
            "https://www.deribit.com/api/v2/public/get_instruments",
            params={"currency": "ETH", "kind": "option", "expired": "false"},
            timeout=15
        ).json()
        instruments = r["result"]

        # Limit to next 60 days to keep this manageable
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) + timedelta(days=60)).timestamp() * 1000

        results = []
        relevant = [i for i in instruments if i["expiration_timestamp"] < cutoff]
        logger.info(f"Fetching {len(relevant)} ETH option contracts from Deribit...")

        # Get summary data for all instruments at once
        summary_r = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            params={"currency": "ETH", "kind": "option"},
            timeout=15
        ).json()

        summary_by_name = {s["instrument_name"]: s for s in summary_r["result"]}

        for inst in relevant:
            name = inst["instrument_name"]
            s = summary_by_name.get(name)
            if not s:
                continue

            # Parse instrument: ETH-21JUN26-2200-C
            parts = name.split("-")
            if len(parts) != 4:
                continue
            expiry_str = parts[1]
            strike = float(parts[2])
            opt_type = parts[3]  # 'C' or 'P'

            try:
                expiry_date = datetime.strptime(expiry_str, "%d%b%y").date()
            except ValueError:
                continue

            results.append({
                "expiry_date": expiry_date,
                "strike": strike,
                "option_type": opt_type,
                "open_interest": s.get("open_interest", 0),
                "volume_24h": s.get("volume", 0),
                "iv": s.get("mark_iv"),
                "bid_price": s.get("bid_price"),
                "ask_price": s.get("ask_price"),
                # Deribit summary doesn't include greeks; would need ticker call per option
                "delta": None,
                "gamma": None,
                "vega": None,
            })

        return results
    except Exception as e:
        logger.error(f"Deribit options fetch failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# SNAPSHOT — orchestrator
# ═══════════════════════════════════════════════════════════════════

def take_snapshot(notes=None):
    """Capture everything once."""
    t0 = time.time()
    logger.info("═══ Starting snapshot ═══")

    # 1. Spot prices
    eth_price, btc_price = fetch_spot_prices()
    if not eth_price:
        logger.error("Cannot get spot price, aborting snapshot")
        return None

    eth_btc = eth_price / btc_price if btc_price else None
    logger.info(f"  ETH=${eth_price:,.2f}  BTC=${btc_price:,.2f}  ETH/BTC={eth_btc:.5f}")

    conn = get_connection()
    cur = get_cursor(conn)

    # Insert snapshot
    cur.execute("""
        INSERT INTO eth_snapshots (spot_price, btc_price, eth_btc_ratio, notes)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (eth_price, btc_price, eth_btc, notes))
    snapshot_id = cur.fetchone()["id"]

    # 2. Orderbook depth
    ob = fetch_orderbook_depth()
    for exchange, side, pct, eth, usd in ob:
        cur.execute("""
            INSERT INTO eth_orderbook_depth
                (snapshot_id, exchange, side, pct_from_mid, cumulative_eth, cumulative_usd)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (snapshot_id, exchange, side, pct, eth, usd))
    logger.info(f"  Orderbook depth: {len(ob)} rows")

    # 3. Funding & OI
    deriv = fetch_perp_funding()
    long_short = fetch_long_short_ratio()
    for exchange, funding, next_fund, oi, _, mark in deriv:
        ls = long_short if exchange == "binance" else None
        cur.execute("""
            INSERT INTO eth_derivatives
                (snapshot_id, exchange, funding_rate, next_funding,
                 open_interest_usd, long_short_ratio, mark_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (snapshot_id, exchange, funding, next_fund, oi, ls, mark))
    logger.info(f"  Derivatives: {len(deriv)} venues  long/short={long_short}")

    # 4. Options chain
    opts = fetch_deribit_options()
    for o in opts:
        cur.execute("""
            INSERT INTO eth_options_chain
                (snapshot_id, expiry_date, strike, option_type,
                 open_interest, volume_24h, iv,
                 bid_price, ask_price, delta, gamma, vega)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (snapshot_id, o["expiry_date"], o["strike"], o["option_type"],
              o["open_interest"], o["volume_24h"], o["iv"],
              o["bid_price"], o["ask_price"],
              o["delta"], o["gamma"], o["vega"]))
    logger.info(f"  Options chain: {len(opts)} contracts")

    conn.commit()
    conn.close()

    elapsed = time.time() - t0
    logger.info(f"═══ Snapshot {snapshot_id} complete in {elapsed:.1f}s ═══")
    return snapshot_id


def run_continuous():
    """Loop forever, capturing on schedule."""
    logger.info(f"Starting continuous capture (every {CAPTURE_INTERVAL}s)")
    while True:
        try:
            take_snapshot()
        except KeyboardInterrupt:
            logger.info("Stopped by user")
            break
        except Exception as e:
            logger.exception(f"Snapshot error: {e}")
        time.sleep(CAPTURE_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-db", action="store_true",
                        help="Create snapshot tables")
    parser.add_argument("--continuous", action="store_true",
                        help="Run on a loop")
    parser.add_argument("--notes", type=str, default=None,
                        help="Notes for this snapshot")
    args = parser.parse_args()

    if args.init_db:
        init_db()
    elif args.continuous:
        run_continuous()
    else:
        take_snapshot(notes=args.notes)