"""
Auto-Label Headlines v2 — Cause vs Effect Aware
=================================================

Major improvements over v1:
  1. CATEGORY DETECTION: keyword-based pre-classification determines
     whether a headline CAN cause a market move (geopolitical, regulatory,
     macro) or is merely describing/analyzing one (technical_analysis,
     price_commentary, opinion).

  2. INCREMENTAL PRICE IMPACT: measures the price move AFTER the headline
     relative to the trend BEFORE it. If BTC was already falling 2%/hour,
     a -3% move in the next hour is only -1% incremental.

  3. EVENT CLUSTERING: when many headlines share the same date and similar
     price impact, only the earliest ones are labelled "potentially_causal".
     Later ones are marked "during_event" (reactive coverage).

  4. CAUSAL PLAUSIBILITY: combines category + timing + incremental move
     to determine if this headline plausibly caused (or contributed to)
     the observed price action, vs merely reporting on it.

Usage:
    python -m labelling.auto_label_v2
    python -m labelling.auto_label_v2 --relabel   (clear and redo all labels)
"""

import os
import sys
import re
import argparse
import logging
from datetime import timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CATEGORY DETECTION — keyword-based pre-classification
# ═══════════════════════════════════════════════════════════════════
# Priority order matters: first match wins.
# Categories that CAN cause moves are "causal_capable".
# Categories that describe/analyze are "reactive_only".

CATEGORY_RULES = [
    # ── Causal-capable categories ──────────────────────────────────
    {
        "category": "geopolitical",
        "causal_capable": True,
        "patterns": [
            r"\b(war|ceasefire|sanction|tariff|embargo|invasion|missile|strike|military|troops)\b",
            r"\b(iran|russia|china|north korea|ukraine)\b.*\b(attack|threat|escalat|conflict)\b",
            r"\btrump\b.*\b(announc|order|sign|tariff|ban|threaten)\b",
            r"\b(geopolit|trade war|cold war|strait of hormuz)\b",
            r"\b(greenland|nato|liberation day)\b",
        ],
    },
    {
        "category": "regulatory",
        "causal_capable": True,
        "patterns": [
            r"\b(sec |sec\b).*\b(approv|reject|sue|charg|investigat|settlement|fine)\b",
            r"\b(etf)\b.*\b(approv|reject|fil|delay|launch)\b",
            r"\b(ban|prohibit|restrict|outlaw).*\b(crypto|bitcoin|mining|stablecoin)\b",
            r"\b(regulat|legislat|bill|law|act)\b.*\b(crypto|bitcoin|digital asset|stablecoin)\b",
            r"\b(cftc|doj|fbi|treasury|ofac|fincen)\b.*\b(crypto|bitcoin|exchange)\b",
            r"\b(mica|mica)\b",
        ],
    },
    {
        "category": "macro",
        "causal_capable": True,
        "patterns": [
            r"\b(federal reserve|fed |the fed\b|fomc|powell)\b",
            r"\b(interest rate|rate cut|rate hike|rate decision|rate hold|basis point)\b",
            r"\b(cpi|inflation|ppi|gdp|employment|payroll|jobless|recession)\b",
            r"\b(dollar|dxy|treasury|yield|bond)\b.*\b(surge|crash|spike|plunge|soar)\b",
            r"\b(quantitative|qt|qe|tightening|easing)\b",
        ],
    },
    {
        "category": "exchange_event",
        "causal_capable": True,
        "patterns": [
            r"\b(hack|exploit|breach|stolen|drain|rug pull|rugpull)\b",
            r"\b(insolvenc|bankrupt|collapse|shut down|halt|suspend)\b.*\b(exchange|withdraw|trading)\b",
            r"\b(binance|coinbase|kraken|okx|bybit|ftx|gemini)\b.*\b(halt|suspend|investigate|charged|sued)\b",
            r"\b(depeg|depegged|lost peg)\b",
            r"\b(luna|terra|ust)\b.*\b(crash|collapse|depeg)\b",
            r"\b(liquidat)\b.*\b(\$\d+\s*(m|b|million|billion))\b",
        ],
    },
    {
        "category": "whale_flow",
        "causal_capable": True,
        "patterns": [
            r"\b(whale|whales)\b.*\b(buy|sell|dump|accumul|transfer|mov)\b",
            r"\b(mt\.?\s*gox|silk road|government)\b.*\b(transfer|mov|send|sell|btc|bitcoin)\b",
            r"\b(etf)\b.*\b(inflow|outflow|redeem|billion|million)\b",
            r"\b(tether|usdt|usdc)\b.*\b(mint|burn|print|issu)\b",
        ],
    },
    {
        "category": "protocol",
        "causal_capable": True,
        "patterns": [
            r"\b(upgrade|hard fork|soft fork|merge|pectra|dencun|shanghai)\b",
            r"\b(vulnerability|exploit|bug|critical)\b.*\b(found|discover|patch|fix)\b",
            r"\b(ethereum|bitcoin|solana)\b.*\b(network|outage|down|halt)\b",
        ],
    },

    # ── Reactive-only categories (cannot cause moves) ──────────────
    {
        "category": "technical_analysis",
        "causal_capable": False,
        "patterns": [
            r"\btechnical analysis\b",
            r"\b(rsi|macd|bollinger|fibonacci|moving average|support resistance)\b",
            r"\b(bull flag|bear flag|head and shoulders|double top|double bottom|wedge|triangle)\b",
            r"\b(overbought|oversold|momentum indicator)\b",
            r"\b(chart|pattern)\b.*\b(show|suggest|signal|indicate|form)\b",
        ],
    },
    {
        "category": "price_commentary",
        "causal_capable": False,
        "patterns": [
            r"\bprice prediction\b",
            r"\b(could|may|might|will)\b.*\b(reach|hit|surge|pump|moon|rally)\b.*\b\$\d+",
            r"\b(analyst|expert|trader)\b.*\b(say|predict|forecast|expect|believe|think)\b",
            r"\b(bull case|bear case|price target|prediction)\b",
            r"\b(why|what|how)\b.*\b(bounc|crash|fall|rise|drop|pump|dump)\b",
        ],
    },
    {
        "category": "opinion",
        "causal_capable": False,
        "patterns": [
            r"\b(opinion|editorial|column|commentary|take|perspective)\b",
            r"\b(i think|we believe|in my view|it seems|arguably)\b",
            r"\b(should you|is it time|is it worth|should i)\b",
        ],
    },
    {
        "category": "promotion",
        "causal_capable": False,
        "patterns": [
            r"\b(airdrop|giveaway|presale|ico|ieo|ido|launchpad)\b",
            r"\b(memecoin|meme coin|shib|pepe|doge)\b.*\b(100x|1000x|pump|moon|gem)\b",
            r"\b(best crypto|top crypto|crypto to buy|next big|hidden gem)\b",
            r"\b(sponsored|partner|ad|advertisement)\b",
        ],
    },
]


def classify_headline_category(title: str) -> tuple:
    """
    Classify a headline by category using keyword patterns.
    
    Returns:
        (category, is_causal_capable)
    """
    title_lower = title.lower()
    
    for rule in CATEGORY_RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, title_lower):
                return rule["category"], rule["causal_capable"]
    
    # Default: uncategorized, assume potentially causal (conservative)
    return "uncategorized", True


# ═══════════════════════════════════════════════════════════════════
# PRICE IMPACT — incremental measurement
# ═══════════════════════════════════════════════════════════════════

def get_price_at(cur, symbol, target_ts, tolerance_minutes=60):
    """Find the closest price to a target timestamp."""
    cur.execute("""
        SELECT close, timestamp
        FROM price_snapshots
        WHERE symbol = %s
          AND timestamp BETWEEN %s - INTERVAL '%s minutes'
                             AND %s + INTERVAL '%s minutes'
        ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s)))
        LIMIT 1
    """, (symbol, target_ts, tolerance_minutes, target_ts, tolerance_minutes, target_ts))
    row = cur.fetchone()
    return float(row["close"]) if row else None


def compute_move(cur, symbol, base_ts, base_price, hours):
    """Compute max absolute move in a time window."""
    cur.execute("""
        SELECT MIN(low) as min_low, MAX(high) as max_high
        FROM price_snapshots
        WHERE symbol = %s
          AND timestamp > %s
          AND timestamp <= %s + INTERVAL '%s hours'
    """, (symbol, base_ts, base_ts, hours))
    row = cur.fetchone()
    if not row or row["min_low"] is None:
        return None, None

    up = (float(row["max_high"]) - base_price) / base_price
    down = (base_price - float(row["min_low"])) / base_price

    if abs(up) >= abs(down):
        return up, "bullish"
    else:
        return -down, "bearish"


def compute_pre_move(cur, symbol, base_ts, base_price, hours_before=4):
    """
    Measure the trend BEFORE the headline.
    Returns the % move in the N hours leading up to publication.
    This is used to compute incremental impact.
    """
    cur.execute("""
        SELECT close
        FROM price_snapshots
        WHERE symbol = %s
          AND timestamp >= %s - INTERVAL '%s hours'
          AND timestamp < %s
        ORDER BY timestamp ASC
        LIMIT 1
    """, (symbol, base_ts, hours_before, base_ts))
    row = cur.fetchone()
    if not row:
        return 0.0
    
    earlier_price = float(row["close"])
    if earlier_price == 0:
        return 0.0
    return (base_price - earlier_price) / earlier_price


def classify_impact(move_48h, incremental_4h, is_causal):
    """
    Classify impact level considering category and incremental move.
    
    Non-causal headlines get downgraded regardless of price action.
    Causal headlines use incremental move for the first 4h,
    then total move for 48h (because they could trigger chain reactions).
    """
    if not is_causal:
        # Technical analysis, price commentary, opinion, promotion
        # These CANNOT cause moves — label by how interesting they are
        # for the model to learn, but cap at "low"
        abs_48h = abs(move_48h) if move_48h else 0
        if abs_48h >= 0.02:
            return "during_event"  # published during a move it didn't cause
        return "noise"
    
    # For causal-capable headlines, use incremental 4h move as primary
    # (filters out headlines published mid-crash that didn't cause it)
    abs_incr = abs(incremental_4h) if incremental_4h else 0
    abs_48h = abs(move_48h) if move_48h else 0
    
    # Must show meaningful incremental impact in first 4 hours
    if abs_incr >= 0.03 and abs_48h >= 0.05:
        return "high"
    elif abs_incr >= 0.015 and abs_48h >= 0.02:
        return "medium"
    elif abs_incr >= 0.005:
        return "low"
    elif abs_48h >= 0.05:
        # Large 48h move but small incremental = published during event, not before
        return "during_event"
    return "noise"


# ═══════════════════════════════════════════════════════════════════
# EVENT CLUSTERING — deduplicate same-day causal attribution
# ═══════════════════════════════════════════════════════════════════

def cluster_and_mark_events(cur, conn):
    """
    Post-processing: for each day with high-impact labels,
    only keep the earliest N headlines as "potentially_causal".
    Mark later ones as "during_event".
    
    This prevents 50 headlines from Feb 4 all being labelled "high".
    """
    logger.info("Clustering events by day...")
    
    cur.execute("""
        SELECT DATE(h.published_at) as pub_date,
               COUNT(*) as cnt
        FROM headline_labels l
        JOIN headlines h ON h.id = l.headline_id
        WHERE l.impact_level IN ('high', 'medium')
        GROUP BY DATE(h.published_at)
        HAVING COUNT(*) > 5
        ORDER BY pub_date
    """)
    hot_days = cur.fetchall()
    
    demoted = 0
    for day_row in hot_days:
        pub_date = day_row["pub_date"]
        
        # Get all high/medium headlines for this day, ordered by time
        cur.execute("""
            SELECT l.id as label_id, h.title, h.published_at,
                   l.impact_level, l.category, l.is_causal_capable
            FROM headline_labels l
            JOIN headlines h ON h.id = l.headline_id
            WHERE DATE(h.published_at) = %s
              AND l.impact_level IN ('high', 'medium')
            ORDER BY h.published_at ASC
        """, (pub_date,))
        day_headlines = cur.fetchall()
        
        if len(day_headlines) <= 5:
            continue
        
        # Keep first 3 causal-capable headlines, demote the rest
        causal_kept = 0
        for hl in day_headlines:
            if hl["is_causal_capable"] and causal_kept < 3:
                causal_kept += 1
                continue  # keep this one
            else:
                # Demote to during_event
                cur.execute("""
                    UPDATE headline_labels
                    SET impact_level = 'during_event'
                    WHERE id = %s AND impact_level IN ('high', 'medium')
                """, (hl["label_id"],))
                if cur.rowcount > 0:
                    demoted += 1
        
        conn.commit()
    
    logger.info(f"Event clustering: demoted {demoted} headlines to 'during_event'")


# ═══════════════════════════════════════════════════════════════════
# MAIN LABELLING PIPELINE
# ═══════════════════════════════════════════════════════════════════

def auto_label_v2(limit=None, symbol="BTCUSDT", relabel=False):
    """
    Auto-label headlines with category-aware, incremental impact scoring.
    """
    conn = get_connection()
    cur = get_cursor(conn)

    # Optionally clear existing labels
    if relabel:
        cur.execute("DELETE FROM headline_labels")
        conn.commit()
        logger.info("Cleared all existing labels for re-labelling")

    # Find unlabelled headlines
    query = """
        SELECT h.id, h.title, h.published_at, h.source_name
        FROM headlines h
        LEFT JOIN headline_labels l ON h.id = l.headline_id
        WHERE l.id IS NULL
        ORDER BY h.published_at
    """
    if limit:
        query += f" LIMIT {limit}"

    cur.execute(query)
    headlines = cur.fetchall()
    logger.info(f"Found {len(headlines)} unlabelled headlines")

    labelled = 0
    skipped = 0
    category_counts = defaultdict(int)
    impact_counts = defaultdict(int)

    for i, h in enumerate(headlines):
        headline_id = h["id"]
        pub_ts = h["published_at"]
        title = h["title"]

        # Step 1: Classify category
        category, is_causal = classify_headline_category(title)
        category_counts[category] += 1

        # Step 2: Get price at publication time
        base_price = get_price_at(cur, symbol, pub_ts)
        if base_price is None:
            skipped += 1
            continue

        # Step 3: Compute post-publication moves
        moves = {}
        direction = "neutral"
        for hours in [1, 4, 24, 48]:
            move_pct, move_dir = compute_move(cur, symbol, pub_ts, base_price, hours)
            moves[hours] = move_pct
            if hours == 4 and move_dir:
                direction = move_dir  # use 4h direction, not 48h

        # Step 4: Compute pre-move (trend before headline)
        pre_move = compute_pre_move(cur, symbol, pub_ts, base_price, hours_before=4)

        # Step 5: Compute incremental impact
        # incremental = post_move - pre_trend (how much did this headline ADD)
        post_4h = moves.get(4, 0) or 0
        incremental_4h = post_4h - pre_move  # subtract existing trend

        # Step 6: Classify impact with category awareness
        move_48h = moves.get(48)
        impact_level = classify_impact(move_48h, incremental_4h, is_causal)
        impact_counts[impact_level] += 1

        try:
            cur.execute("""
                INSERT INTO headline_labels
                    (headline_id, price_move_1h, price_move_4h,
                     price_move_24h, price_move_48h,
                     impact_level, impact_direction,
                     category, is_causal_capable,
                     pre_move_4h, incremental_move_4h)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (headline_id) DO UPDATE SET
                    price_move_1h = EXCLUDED.price_move_1h,
                    price_move_4h = EXCLUDED.price_move_4h,
                    price_move_24h = EXCLUDED.price_move_24h,
                    price_move_48h = EXCLUDED.price_move_48h,
                    impact_level = EXCLUDED.impact_level,
                    impact_direction = EXCLUDED.impact_direction,
                    category = EXCLUDED.category,
                    is_causal_capable = EXCLUDED.is_causal_capable,
                    pre_move_4h = EXCLUDED.pre_move_4h,
                    incremental_move_4h = EXCLUDED.incremental_move_4h
            """, (
                headline_id,
                moves.get(1), moves.get(4),
                moves.get(24), moves.get(48),
                impact_level, direction,
                category, is_causal,
                pre_move, incremental_4h,
            ))
            labelled += 1
        except Exception as e:
            logger.warning(f"Label error for headline {headline_id}: {e}")
            conn.rollback()

        if (i + 1) % 2000 == 0:
            conn.commit()
            logger.info(f"  Progress: {i+1}/{len(headlines)}, labelled: {labelled}")

    conn.commit()

    # Step 7: Event clustering — demote duplicate causal attributions
    cluster_and_mark_events(cur, conn)

    # ── Summary ────────────────────────────────────────────────────
    logger.info(f"\n{'='*70}")
    logger.info(f"Labelling complete: {labelled} labelled, {skipped} skipped")

    logger.info(f"\nCategory distribution:")
    for cat, cnt in sorted(category_counts.items(), key=lambda x: -x[1]):
        causal_tag = "causal" if any(
            r["category"] == cat and r["causal_capable"]
            for r in CATEGORY_RULES
        ) else "reactive"
        logger.info(f"  {cat:25s}: {cnt:>6d}  ({causal_tag})")

    logger.info(f"\nImpact distribution (after clustering):")
    cur.execute("""
        SELECT impact_level, COUNT(*) as cnt
        FROM headline_labels
        GROUP BY impact_level
        ORDER BY cnt DESC
    """)
    for row in cur.fetchall():
        logger.info(f"  {row['impact_level']:15s}: {row['cnt']:>6d}")

    # Show top causal headlines
    cur.execute("""
        SELECT h.title, l.impact_level, l.impact_direction, l.category,
               l.incremental_move_4h, l.price_move_48h, h.published_at
        FROM headline_labels l
        JOIN headlines h ON h.id = l.headline_id
        WHERE l.impact_level = 'high'
          AND l.is_causal_capable = TRUE
        ORDER BY ABS(l.incremental_move_4h) DESC
        LIMIT 15
    """)
    results = cur.fetchall()
    if results:
        logger.info(f"\nTop 15 CAUSAL high-impact headlines:")
        for row in results:
            incr = row["incremental_move_4h"] or 0
            total = row["price_move_48h"] or 0
            logger.info(
                f"  [{row['published_at'].strftime('%Y-%m-%d %H:%M')}] "
                f"{row['category']:15s} incr={incr:+.1%} total={total:+.1%} | "
                f"{row['title'][:70]}"
            )

    # Show what got correctly filtered out
    cur.execute("""
        SELECT l.category, COUNT(*) as cnt
        FROM headline_labels l
        WHERE l.is_causal_capable = FALSE
          AND ABS(l.price_move_48h) > 0.05
        GROUP BY l.category
        ORDER BY cnt DESC
    """)
    filtered = cur.fetchall()
    if filtered:
        logger.info(f"\nCorrectly filtered (big move day, but reactive headline):")
        for row in filtered:
            logger.info(f"  {row['category']:25s}: {row['cnt']:>4d} headlines downgraded")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-label headlines v2 (cause-effect aware)")
    parser.add_argument("--limit", type=int, help="Max headlines to label")
    parser.add_argument("--symbol", type=str, default="BTCUSDT", help="Price symbol")
    parser.add_argument("--relabel", action="store_true", help="Clear and redo all labels")
    args = parser.parse_args()

    auto_label_v2(limit=args.limit, symbol=args.symbol, relabel=args.relabel)
