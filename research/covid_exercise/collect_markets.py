"""
Historical Market Data Collector
=================================

Pulls daily OHLCV for the COVID research period and control periods.
Tries yfinance first, falls back to Stooq (no auth, more reliable).

Stores to historical_market_daily.

Tickers:
  SPY     - S&P 500 ETF (broad equity)
  QQQ     - Nasdaq 100 (tech-heavy equity)
  ^VIX    - CBOE Volatility Index
  TLT     - 20+ year Treasury (safe haven)
  GLD     - Gold ETF (safe haven)
  CL=F    - Crude oil futures
  BTC-USD - Bitcoin (where available)
  ^DJI    - Dow Jones Industrial Average

Usage:
    python -m research.covid_exercise.collect_markets \\
        --start 2019-09-01 --end 2020-06-30
"""

import os
import sys
import argparse
import logging
import platform
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Tickers to pull, with stooq fallback symbols
# Stooq uses different naming: ^spx (S&P), ^vix, etc., crypto via .v suffix
TICKERS = [
    {"yfinance": "SPY",     "stooq": "spy.us",     "label": "SPY"},
    {"yfinance": "QQQ",     "stooq": "qqq.us",     "label": "QQQ"},
    {"yfinance": "^VIX",    "stooq": "^vix",       "label": "^VIX"},
    {"yfinance": "TLT",     "stooq": "tlt.us",     "label": "TLT"},
    {"yfinance": "GLD",     "stooq": "gld.us",     "label": "GLD"},
    {"yfinance": "CL=F",    "stooq": "cl.f",       "label": "CL=F"},
    {"yfinance": "BTC-USD", "stooq": "btcusd",     "label": "BTC-USD"},
    {"yfinance": "^DJI",    "stooq": "^dji",       "label": "^DJI"},
    {"yfinance": "^GSPC",   "stooq": "^spx",       "label": "^GSPC"},
]


def pip_install(package: str) -> bool:
    """Install a package using whichever flag the OS supports."""
    import subprocess
    
    # Try with --break-system-packages first (Linux/Mac newer pythons)
    # Fall back to plain pip install (Windows, older pythons)
    cmds = [
        [sys.executable, "-m", "pip", "install", package, "--break-system-packages", "--quiet"],
        [sys.executable, "-m", "pip", "install", package, "--quiet"],
    ]
    
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    
    log.error(f"Could not install {package}. Please run: pip install {package}")
    return False


def install_yfinance_if_needed():
    try:
        import yfinance  # noqa
        return True
    except ImportError:
        log.info("yfinance not found, installing...")
        return pip_install("yfinance")


def install_pandas_datareader_if_needed():
    """No longer needed - we use direct Stooq CSV API."""
    return True


def fetch_yfinance(ticker: str, start: str, end: str):
    """
    Try yfinance. Returns a list of dicts in the same shape as fetch_stooq_csv:
    [{date, open, high, low, close, volume, daily_return}, ...]
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(
            start=start, end=end, auto_adjust=False
        )
        if hist.empty:
            return None
        
        rows = []
        for date_idx, row in hist.iterrows():
            trade_date = date_idx.date() if hasattr(date_idx, 'date') else date_idx
            rows.append({
                "date": trade_date,
                "open": float(row["Open"]) if not _is_nan(row["Open"]) else None,
                "high": float(row["High"]) if not _is_nan(row["High"]) else None,
                "low": float(row["Low"]) if not _is_nan(row["Low"]) else None,
                "close": float(row["Close"]) if not _is_nan(row["Close"]) else None,
                "volume": int(row["Volume"]) if "Volume" in row and not _is_nan(row["Volume"]) else None,
            })
        
        # Compute daily returns
        prev_close = None
        for row in rows:
            if prev_close is not None and row["close"] is not None:
                row["daily_return"] = (row["close"] / prev_close) - 1
            else:
                row["daily_return"] = None
            if row["close"] is not None:
                prev_close = row["close"]
        
        return rows
    except Exception as e:
        log.debug(f"yfinance failed for {ticker}: {e}")
        return None


def fetch_stooq_csv(symbol: str, start: str, end: str):
    """
    Fetch daily OHLCV from Stooq via direct CSV API. No third-party
    library — just requests + standard CSV parsing. Bypasses the
    pandas-datareader compatibility issue.
    
    Stooq URL format:
      https://stooq.com/q/d/l/?s=SYMBOL&d1=YYYYMMDD&d2=YYYYMMDD&i=d
    
    Returns a list of dicts with date/OHLCV or None on failure.
    """
    import requests
    from datetime import datetime as dt
    
    # Convert YYYY-MM-DD to YYYYMMDD
    d1 = start.replace("-", "")
    d2 = end.replace("-", "")
    
    url = "https://stooq.com/q/d/l/"
    params = {
        "s": symbol,
        "d1": d1,
        "d2": d2,
        "i": "d",  # daily
    }
    
    try:
        r = requests.get(url, params=params, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.debug(f"Stooq HTTP {r.status_code} for {symbol}")
            return None
        
        text = r.text.strip()
        if not text or "No data" in text or text.lower().startswith("no data"):
            return None
        
        lines = text.split("\n")
        if len(lines) < 2:
            return None
        
        # Stooq CSV header: Date,Open,High,Low,Close,Volume
        header = [h.strip() for h in lines[0].split(",")]
        if "Date" not in header[0]:
            log.debug(f"Stooq unexpected header for {symbol}: {header}")
            return None
        
        rows = []
        for line in lines[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                trade_date = dt.strptime(parts[0], "%Y-%m-%d").date()
                open_p = float(parts[1]) if parts[1] else None
                high_p = float(parts[2]) if parts[2] else None
                low_p = float(parts[3]) if parts[3] else None
                close_p = float(parts[4]) if parts[4] else None
                volume = int(parts[5]) if len(parts) > 5 and parts[5] else None
                rows.append({
                    "date": trade_date,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                })
            except (ValueError, IndexError):
                continue
        
        if not rows:
            return None
        
        # Sort oldest first
        rows.sort(key=lambda r: r["date"])
        
        # Compute daily returns
        prev_close = None
        for row in rows:
            if prev_close is not None and row["close"] is not None:
                row["daily_return"] = (row["close"] / prev_close) - 1
            else:
                row["daily_return"] = None
            if row["close"] is not None:
                prev_close = row["close"]
        
        return rows
    
    except Exception as e:
        log.debug(f"Stooq fetch failed for {symbol}: {e}")
        return None


def fetch_stooq(symbol: str, start: str, end: str):
    """Wrapper to maintain old interface."""
    return fetch_stooq_csv(symbol, start, end)


def collect(start: str, end: str):
    have_yf = install_yfinance_if_needed()
    
    conn = get_connection()
    cur = get_cursor(conn)
    
    for t in TICKERS:
        label = t["label"]
        log.info(f"Fetching {label} from {start} to {end}...")
        
        rows = None
        source = None
        
        if have_yf:
            rows = fetch_yfinance(t["yfinance"], start, end)
            if rows:
                source = "yfinance"
        
        if not rows:
            log.info(f"  yfinance failed, trying Stooq ({t['stooq']})...")
            rows = fetch_stooq_csv(t["stooq"], start, end)
            if rows:
                source = "stooq"
        
        if not rows:
            log.warning(f"  No data returned for {label}")
            continue
        
        log.info(f"  Got {len(rows)} rows from {source}")
        
        inserted = 0
        for row in rows:
            try:
                cur.execute("""
                    INSERT INTO historical_market_daily
                        (ticker, trade_date, open_price, high_price, low_price,
                         close_price, adj_close, volume, daily_return)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker, trade_date) DO UPDATE SET
                        open_price = EXCLUDED.open_price,
                        high_price = EXCLUDED.high_price,
                        low_price = EXCLUDED.low_price,
                        close_price = EXCLUDED.close_price,
                        adj_close = EXCLUDED.adj_close,
                        volume = EXCLUDED.volume,
                        daily_return = EXCLUDED.daily_return
                """, (
                    label, row["date"],
                    row["open"], row["high"], row["low"],
                    row["close"], row["close"],  # adj_close = close (Stooq doesn't separate)
                    row["volume"],
                    row["daily_return"],
                ))
                inserted += 1
            except Exception as e:
                log.warning(f"  Insert failed for {label} {row['date']}: {e}")
        
        conn.commit()
        log.info(f"  → {inserted} rows for {label}")
    
    conn.close()


def _is_nan(v):
    """Cross-platform NaN check."""
    try:
        return v != v
    except TypeError:
        return v is None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    
    collect(args.start, args.end)


if __name__ == "__main__":
    main()

