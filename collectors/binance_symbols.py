"""
Enumerate every symbol Binance has EVER archived, for point-in-time universe
construction.

WHY THIS EXISTS
---------------
`FINDINGS_survivorship.md` established that the momentum result is materially
inflated by survivorship, but that the MAGNITUDE IS UNIDENTIFIED — because
adding hand-picked dead tokens to a hand-picked survivor list changes which
names rank into the top-K at every rebalance. That is not a correction, it is
a different universe.

The fix is a **point-in-time universe**: on each rebalance date, rank only the
tokens that were actually liquid and listed *on that date*. That requires
knowing (a) every symbol that ever traded, and (b) its volume over time.

Both are in the public archive. `data.binance.vision` is an S3 bucket, and S3
exposes a listing API that returns one `CommonPrefixes` entry per symbol
directory. That is the enumeration a data vendor would otherwise sell you.

STEP 1 ONLY
-----------
This script does enumeration and nothing else, deliberately. Volume history
for ~700 symbols across ~70 months is tens of thousands of downloads; find out
how many symbols actually exist BEFORE committing to that.

USAGE
-----
    python -m collectors.binance_symbols --enumerate
    python -m collectors.binance_symbols --enumerate --quote USDT
    python -m collectors.binance_symbols --enumerate --save symbols.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("binance_symbols")

PREFIX = "data/spot/monthly/klines/"

# The bucket is reachable several ways and the working form has changed over
# time. Try each; report which one answered so it can be hard-coded later.
ENDPOINTS = [
    "https://data.binance.vision",
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision",
    "https://data.binance.vision.s3.amazonaws.com",
]

S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

# ---------------------------------------------------------------------
# EXCLUSIONS — these would silently corrupt a point-in-time universe.
#
# The archive contains far more than spot crypto pairs. Two categories
# are actively dangerous for a momentum ranker:
#
#   LEVERAGED TOKENS (…UPUSDT / …DOWNUSDT) — Binance's discontinued 3x
#   products. A trailing-return ranker would select them at the top of
#   every rally BY CONSTRUCTION, since they are geared. They are not
#   spot assets and they no longer exist. Including them produces a
#   spectacular, entirely fake result.
#
#   TOKENIZED STOCKS (AAPLBUSDT, AAOIBUSDT, …) — equity exposure, also
#   discontinued, not the asset class under test.
#
# Both are visible in the raw listing, so the filter must be explicit.
# ---------------------------------------------------------------------
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "UPUSDC", "DOWNUSDC",
                      "UPBUSD", "DOWNBUSD")

# Tokenized equities traded against BUSD with a trailing 'B' convention.
TOKENIZED_STOCKS = {
    "AAPLBUSDT", "AAOIBUSDT", "TSLABUSDT", "COINBUSDT", "MSTRBUSDT",
    "MSFTBUSDT", "NVDABUSDT", "AMZNBUSDT", "GOOGLBUSDT", "NFLXBUSDT",
    "BABABUSDT", "PYPLBUSDT", "MRNABUSDT",
}

# Stablecoin-vs-stablecoin pairs: no directional return to rank on.
STABLE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "PAX", "DAI",
                "EUR", "GBP", "AUD", "TRY", "BRL", "RUB", "JPY", "IDR",
                "NGN", "ZAR", "UAH", "BIDR", "BKRW", "BVND"}


def is_tradeable_spot(sym: str, quote: str = "USDT") -> bool:
    """Exclude instruments that are not spot crypto vs `quote`."""
    if not sym.endswith(quote):
        return False
    if sym.endswith(LEVERAGED_SUFFIXES):
        return False
    if sym in TOKENIZED_STOCKS:
        return False
    base = sym[: -len(quote)]
    if not base or base in STABLE_BASES:
        return False
    return True


def _parse_listing(xml_text: str) -> tuple[list[str], str | None, bool]:
    """Return (symbols, next_token_or_marker, is_truncated)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], None, False

    syms = []
    for cp in root.findall(f"{S3_NS}CommonPrefixes"):
        p = cp.findtext(f"{S3_NS}Prefix") or ""
        name = p[len(PREFIX):].strip("/")
        if name:
            syms.append(name)

    truncated = (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() == "true"
    # V2 uses NextContinuationToken; V1 uses NextMarker (or last key).
    token = (root.findtext(f"{S3_NS}NextContinuationToken")
             or root.findtext(f"{S3_NS}NextMarker"))
    if truncated and not token and syms:
        token = PREFIX + syms[-1] + "/"      # V1 fallback: marker = last prefix
    return syms, token, truncated


def enumerate_symbols() -> list[str]:
    """List every symbol directory in the spot monthly klines prefix."""
    for base in ENDPOINTS:
        logger.info("trying endpoint: %s", base)
        symbols, token, guard = [], None, 0
        ok = False
        while guard < 60:
            params = {"delimiter": "/", "prefix": PREFIX, "max-keys": "1000"}
            if token:
                # send both; the server ignores the one it doesn't use
                params["continuation-token"] = token
                params["marker"] = token
                params["list-type"] = "2"
            try:
                r = requests.get(base, params=params, timeout=45)
            except Exception as e:
                logger.warning("  request failed: %s", e)
                break
            if r.status_code != 200:
                logger.warning("  HTTP %s", r.status_code)
                break
            if "<ListBucketResult" not in r.text:
                logger.warning("  not an S3 listing (got %d bytes of %s)",
                               len(r.text),
                               "HTML" if "<html" in r.text.lower() else "?")
                break

            batch, token, truncated = _parse_listing(r.text)
            symbols.extend(batch)
            ok = True
            logger.info("  +%d symbols (total %d)%s",
                        len(batch), len(symbols),
                        " …more" if truncated else "")
            if not truncated or not batch:
                break
            guard += 1

        if ok and symbols:
            logger.info("SUCCESS via %s — %d symbols", base, len(symbols))
            return sorted(set(symbols))

    logger.error("No endpoint returned an S3 listing. The bucket may require "
                 "a different form; open one of these in a browser to see "
                 "what comes back:")
    for b in ENDPOINTS:
        logger.error("  %s?delimiter=/&prefix=%s&max-keys=10", b, PREFIX)
    return []


def main() -> int:
    p = argparse.ArgumentParser(description="Enumerate archived Binance symbols")
    p.add_argument("--enumerate", action="store_true",
                   help="List every archived spot symbol.")
    p.add_argument("--quote", default=None,
                   help="Only symbols with this quote asset, e.g. USDT.")
    p.add_argument("--save", default=None, help="Write the list to a file.")
    args = p.parse_args()

    if not args.enumerate:
        p.print_help()
        return 0

    syms = enumerate_symbols()
    if not syms:
        return 1

    if args.quote:
        q = args.quote.upper()
        raw = [s for s in syms if s.endswith(q)]
        syms = [s for s in raw if is_tradeable_spot(s, q)]
        dropped = [s for s in raw if s not in set(syms)]
        logger.info("%s-quoted: %d raw → %d tradeable spot (%d excluded)",
                    q, len(raw), len(syms), len(dropped))
        if dropped:
            lev = [s for s in dropped if s.endswith(LEVERAGED_SUFFIXES)]
            print(f"\nEXCLUDED {len(dropped)} instruments that would corrupt "
                  f"a momentum ranker:")
            if lev:
                print(f"  {len(lev)} leveraged tokens (…UP/…DOWN) — geared "
                      f"products, would top every rally by construction")
                print("    e.g. " + ", ".join(lev[:6]))
            other = [s for s in dropped if s not in set(lev)]
            if other:
                print(f"  {len(other)} tokenized stocks / stable-vs-stable")
                print("    e.g. " + ", ".join(other[:6]))

    print(f"\n{len(syms)} symbols\n")
    for i in range(0, min(len(syms), 60), 6):
        print("  " + "  ".join(f"{s:<14}" for s in syms[i:i + 6]))
    if len(syms) > 60:
        print(f"  … and {len(syms) - 60} more")

    if args.save:
        with open(args.save, "w") as fh:
            fh.write("\n".join(syms) + "\n")
        logger.info("wrote %s", args.save)

    # Feasibility arithmetic — decide BEFORE launching a bulk download.
    months = 70                      # 2020-11 → 2026-08
    files = len(syms) * months
    print(f"\nFeasibility: {len(syms)} symbols x ~{months} months "
          f"= ~{files:,} monthly 1d-kline files.")
    print(f"At ~0.5 s/file that is ~{files * 0.5 / 3600:.1f} hours of "
          f"downloading.")
    if files > 20000:
        print("\nThat is a lot. Consider restricting the pool: most of these "
              "never had\nmeaningful volume. A quarterly rebalance and a "
              "top-100-ever candidate pool\ngive a defensible point-in-time "
              "universe at a fraction of the cost.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
