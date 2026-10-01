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

# Tokenized equities used a `<TICKER>B` + quote convention (AAPLBUSDT =
# AAPL vs BUSD... then USDT). Hardcoding names does NOT scale — the first
# pass missed AMAT, AMD and ARM. Detect the pattern instead, with a
# known-crypto allowlist so legitimate coins ending in B are kept.
_KNOWN_B_CRYPTO = {
    "BNB", "COMB", "ARB", "BLUR", "JOB", "GLMB",   # legitimate bases
}


def _looks_like_tokenized_stock(base: str) -> bool:
    """`<TICKER>B` pattern: AAPLB, AMATB, AMDB, ARMB, TSLAB…

    Equity tickers are 1-5 uppercase letters followed by the 'B'
    (BUSD-settled) marker. Crypto bases ending in a legitimate B are
    allowlisted above.
    """
    if not base.endswith("B") or len(base) < 3:
        return False
    if base in _KNOWN_B_CRYPTO:
        return False
    stem = base[:-1]
    return 1 <= len(stem) <= 5 and stem.isalpha() and stem.isupper()

# Stablecoin-vs-stablecoin pairs: no directional return to rank on.
STABLE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "PAX", "DAI",
                "EUR", "GBP", "AUD", "TRY", "BRL", "RUB", "JPY", "IDR",
                "NGN", "ZAR", "UAH", "BIDR", "BKRW", "BVND"}


def is_tradeable_spot(sym: str, quote: str = "USDT") -> bool:
    """Exclude instruments that are not spot crypto vs `quote`."""
    # Real Binance tickers are ASCII alphanumerics. Anything else is an
    # artefact of the listing (encoding noise, placeholder entries) and
    # would be untradeable regardless.
    if not sym.isascii() or not sym.isalnum():
        return False
    if not sym.endswith(quote):
        return False
    if sym.endswith(LEVERAGED_SUFFIXES):
        return False
    base = sym[: -len(quote)]
    if not base or base in STABLE_BASES:
        return False
    if _looks_like_tokenized_stock(base):
        return False
    return True


def _parse_listing(xml_text: str, version: str = "v2"
                   ) -> tuple[list[str], str | None, bool]:
    """Return (symbols, next_marker_or_token, is_truncated).

    V1 and V2 paginate differently: V1 wants `marker` = the LAST KEY or
    CommonPrefix returned; V2 wants the server-supplied
    NextContinuationToken. Returning the wrong one causes HTTP 400.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], None, False

    syms, last_prefix = [], None
    for cp in root.findall(f"{S3_NS}CommonPrefixes"):
        p = cp.findtext(f"{S3_NS}Prefix") or ""
        last_prefix = p
        name = p[len(PREFIX):].strip("/")
        if name:
            syms.append(name)

    truncated = (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() == "true"

    if version == "v2":
        token = root.findtext(f"{S3_NS}NextContinuationToken")
    else:
        # V1: server may supply NextMarker; if not (common when using a
        # delimiter), the marker is the last CommonPrefix returned.
        token = root.findtext(f"{S3_NS}NextMarker") or last_prefix
    return syms, token, truncated


def enumerate_symbols() -> list[str]:
    """List every symbol directory in the spot monthly klines prefix.

    Pagination note: S3 ListObjects V1 and V2 take DIFFERENT parameters,
    and sending both causes HTTP 400 on some endpoints. We probe V2
    first, fall back to V1, and never mix them in one request.
    """
    for base in ENDPOINTS:
        for version in ("v2", "v1"):
            logger.info("trying %s (list-type %s)", base, version)
            symbols, token, guard, complete = [], None, 0, False

            while guard < 200:
                params = {"delimiter": "/", "prefix": PREFIX,
                          "max-keys": "1000"}
                if version == "v2":
                    params["list-type"] = "2"
                    if token:
                        params["continuation-token"] = token
                else:
                    if token:
                        params["marker"] = token

                try:
                    r = requests.get(base, params=params, timeout=45)
                except Exception as e:
                    logger.warning("  request failed: %s", e)
                    break
                if r.status_code != 200:
                    logger.warning("  HTTP %s on page %d", r.status_code,
                                   guard + 1)
                    break
                if "<ListBucketResult" not in r.text:
                    logger.warning("  not an S3 listing (%d bytes of %s)",
                                   len(r.text),
                                   "HTML" if "<html" in r.text.lower() else "?")
                    break

                batch, token, truncated = _parse_listing(r.text, version)
                symbols.extend(batch)
                guard += 1
                if guard % 5 == 0 or not truncated:
                    logger.info("  page %d: %d symbols so far%s",
                                guard, len(symbols),
                                "" if truncated else " (complete)")
                if not truncated:
                    complete = True
                    break
                if not batch or not token:
                    logger.warning("  truncated but no continuation marker — "
                                   "pagination stalled at %d", len(symbols))
                    break

            # Only accept a listing we know we finished. A partial listing
            # silently truncates the universe — which is exactly the class
            # of error this whole exercise exists to remove.
            if complete and symbols:
                logger.info("SUCCESS via %s (%s) — %d symbols, %d pages",
                            base, version, len(symbols), guard)
                return sorted(set(symbols))
            if symbols:
                logger.warning("  INCOMPLETE (%d symbols) — discarding and "
                               "trying next method", len(symbols))

    logger.error("No endpoint returned a COMPLETE S3 listing. A partial "
                 "listing is worse than none here: it would silently "
                 "truncate the universe alphabetically.")
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
        # Explicit UTF-8: Windows defaults to cp1252, which cannot encode
        # every symbol in the archive.
        with open(args.save, "w", encoding="utf-8") as fh:
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
