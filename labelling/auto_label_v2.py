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
            r"\b(war|ceasefire|sanction\w*|tariff\w*|embargo|invasion|missile|strike|military|troops)\b",
            r"\b(iran|russia|china|north korea|ukraine|israel|gaza|taiwan)\b.*\b(attack|threat|escalat\w*|conflict|war|strike)\b",
            r"\btrump\b.*\b(announc\w*|order\w*|sign\w*|tariff\w*|ban\w*|threaten\w*|impose\w*|deal)\b",
            r"\b(biden|xi|putin|kim jong)\b.*\b(announc\w*|threat|deal|summit)\b",
            r"\b(geopolit\w*|trade war|cold war|strait of hormuz)\b",
            r"\b(greenland|nato|liberation day|executive order)\b",
            r"\b(election|vote|inauguration)\b.*\b(crypto|bitcoin)\b",
            # Pre-escalation signals — embassies evacuating typically precede
            # military action by hours to days (Russia 2022, Iran 2024)
            r"\b(embassy|embassies|consulate)\b.*\b(evacuat\w*|leave|withdraw|depart|close)\b",
            r"\b(evacuat\w*|urge\w*\s+(to\s+)?leave)\b.*\b(citizens|nationals|personnel|staff)\b",
            r"\b(citizens|nationals)\b.*\b(evacuat\w*|urged to leave)\b",
            r"\b(travel warning|travel advisory|do not travel)\b",
            r"\b(state department|foreign ministry)\b.*\b(warn\w*|evacuat\w*|advisory)\b",
        ],
    },
    {
        "category": "regulatory",
        "causal_capable": True,
        "patterns": [
            r"\b(sec |sec\b|sec's).*\b(approv\w*|reject\w*|sue\w*|charg\w*|investigat\w*|settlement|fine\w*|enforc\w*|file\w*|drop\w*)\b",
            r"\b(etf)\b.*\b(approv\w*|reject\w*|fil\w*|delay\w*|launch\w*|denied|greenlight\w*)\b",
            r"\b(spot|futures) (etf|bitcoin|ethereum)\b",
            r"\b(ban\w*|prohibit\w*|restrict\w*|outlaw\w*|crackdown)\b.*\b(crypto|bitcoin|mining|stablecoin|exchange)\b",
            r"\b(regulat\w*|legislat\w*|bill|law|act|framework|guideline|rule)\b.*\b(crypto|bitcoin|digital asset|stablecoin|blockchain)\b",
            r"\b(cftc|doj|fbi|treasury|ofac|fincen|irs|finra)\b.*\b(crypto|bitcoin|exchange|tax|enforcement)\b",
            r"\b(mica|clarity act|fit21|stablecoin act|genius act)\b",
            r"\b(coinbase|binance|kraken|tether|ripple)\b.*\b(sec|lawsuit|settlement|charged|fined)\b",
            r"\b(senate|congress|house|parliament|european union|eu)\b.*\b(crypto|bitcoin|stablecoin|digital asset)\b",
            r"\b(license|licensing|compliance|aml|kyc)\b.*\b(crypto|exchange|stablecoin)\b",
            r"\b(irs|tax)\b.*\b(crypto|bitcoin|staking|mining|defi)\b",
        ],
    },
    {
        "category": "macro",
        "causal_capable": True,
        "patterns": [
            r"\b(federal reserve|fed |the fed\b|fomc|powell|jerome powell)\b",
            r"\b(interest rate|rate cut|rate hike|rate decision|rate hold|basis point|bps)\b",
            r"\b(cpi|inflation|ppi|gdp|employment|payroll|jobless|unemployment|recession)\b",
            r"\b(dollar|dxy|treasury|yield|bond|10-year|2-year)\b.*\b(surge\w*|crash\w*|spike\w*|plunge\w*|soar\w*|rise\w*|fall\w*)\b",
            r"\b(quantitative|qt|qe|tightening|easing|hawkish|dovish)\b",
            r"\b(nasdaq|s&p|spx|dow jones|stocks)\b.*\b(crash\w*|plunge\w*|rally\w*|correlation|bitcoin)\b",
            r"\b(ecb|boe|boj|pboc)\b.*\b(rate|policy|decision)\b",
            r"\b(gold|oil|commodity)\b.*\b(bitcoin|crypto|correlation|safe haven)\b",
            r"\b(debt ceiling|budget|fiscal|monetary policy)\b",
        ],
    },
    {
        "category": "exchange_event",
        "causal_capable": True,
        "patterns": [
            r"\b(hack\w*|exploit\w*|breach\w*|stolen|drain\w*)\b",
            r"\b(rug pull|rugpull|scam|fraud|ponzi)\b",
            r"\b(insolvenc\w*|bankrupt\w*|collapse\w*|shut down|shutdown|halt\w*|suspend\w*|freeze\w*|frozen)\b",
            # FIX: use halt\w* etc to catch halts/halted/suspending
            r"\b(binance|coinbase|kraken|okx|bybit|ftx|gemini|bitfinex|huobi|mexc|bitget|upbit|bitstamp)\b.*\b(halt\w*|suspend\w*|paus\w*|investigat\w*|charg\w*|sue\w*|hack\w*|exploit\w*|withdraw\w*|outflow\w*|insolven\w*)\b",
            r"\b(withdraw\w*)\b.*\b(halt\w*|suspend\w*|paus\w*|freeze\w*|disabl\w*|unable|issue|delay\w*)\b",
            r"\b(depeg\w*|lost peg|off peg)\b",
            r"\b(luna|terra|ust|celsius|voyager|genesis|three arrows|3ac)\b.*\b(crash\w*|collapse\w*|depeg\w*|bankrupt\w*)\b",
            # FIX: no \b before $ (dollar sign isn't a word char)
            r"\b(liquidat\w*)\b.*(\$\d+|billion|million|cascade|wave)",
            r"(\$\d+\s*(m|b|million|billion))\b.*\b(liquidat\w*|wiped|flash crash)\b",
            r"\b(wiped|wipes)\b.*\$\d+",
            r"\b(flash crash|flash loan attack)\b",
        ],
    },
    {
        "category": "whale_flow",
        "causal_capable": True,
        "patterns": [
            r"\b(whale|whales)\b.*\b(buy|sell|bought|sold|dump\w*|accumul\w*|transfer\w*|mov\w*|deposit\w*|withdraw\w*)\b",
            r"\b(mt\.?\s*gox|silk road|government|us govt|german govt)\b.*\b(transfer\w*|mov\w*|send|sell|btc|bitcoin)\b",
            r"\b(etf)\b.*\b(inflow\w*|outflow\w*|redeem\w*|billion|million|net)\b",
            r"\b(tether|usdt|usdc|dai)\b.*\b(mint\w*|burn\w*|print\w*|issu\w*|supply)\b",
            r"\b(\d+,?\d*\s*btc|\d+k\s*btc|\d+,?\d*\s*eth)\b.*\b(transfer\w*|mov\w*|sold|bought)\b",
            r"\b(institutional|microstrategy|strategy|blackrock|fidelity)\b.*\b(buy\w*|bought|purchase\w*|accumul\w*|sell\w*|sold)\b.*\b(bitcoin|btc|eth)\b",
            r"\b(treasury|corporate)\b.*\b(bitcoin|btc)\b.*\b(purchase\w*|acqui\w*|buy\w*|sell\w*|bought|sold)\b",
        ],
    },
    {
        "category": "protocol",
        "causal_capable": True,
        "patterns": [
            r"\b(upgrade\w*|hard fork|soft fork|merge|pectra|dencun|shanghai|cancun|prague)\b",
            r"\b(vulnerability|exploit\w*|bug|critical)\b.*\b(found|discover\w*|patch\w*|fix\w*|disclos\w*)\b",
            r"\b(ethereum|bitcoin|solana|avalanche|cardano)\b.*\b(network|outage|down|halt\w*|congest\w*)\b",
            r"\b(halving|halvening)\b",
            r"\b(mainnet|testnet)\b.*\b(launch\w*|deploy\w*|live|go live)\b",
            r"\b(consensus|validator|staking)\b.*\b(change\w*|update\w*|vulnerability)\b",
            r"\b(51% attack|double spend)\b",
        ],
    },

    # ── Reactive-only categories (cannot cause moves) ──────────────
    {
        "category": "technical_analysis",
        "causal_capable": False,
        "patterns": [
            r"\btechnical analysis\b",
            r"\b(rsi|macd|bollinger|fibonacci|moving average|support resistance|support and resistance|sma|ema)\b",
            r"\b(bull flag|bear flag|head and shoulders|double top|double bottom|wedge|triangle|pennant|cup and handle)\b",
            r"\b(overbought|oversold|momentum indicator|divergence)\b",
            r"\b(chart|pattern)\b.*\b(show\w*|suggest\w*|signal\w*|indicat\w*|form\w*|reveal\w*)\b",
            r"\b(breakout|breakdown|retest)\b.*\b(above|below|level|resistance|support)\b",
            r"\b(key level|key support|key resistance|critical level)\b",
            r"\b(ta|tech analysis)\b",
            r"\b(golden cross|death cross)\b",
        ],
    },
    {
        "category": "price_commentary",
        "causal_capable": False,
        "patterns": [
            r"\bprice prediction\b",
            # FIX: no \b before $ 
            r"\b(could|may|might|will|can|would)\b.*\b(reach|hit|surge|pump|moon|rally|touch|target)\b.*\$\d+",
            r"\b(could|may|might|will)\b.*\b(reach|hit)\b.*\$",
            r"\b(analyst\w*|expert\w*|trader\w*|strategist\w*)\b.*\b(say\w*|predict\w*|forecast\w*|expect\w*|believ\w*|think\w*|see|target\w*)\b",
            r"\b(bull case|bear case|price target|prediction|outlook|forecast)\b",
            r"\b(why|what|how)\b.*\b(bounc\w*|crash\w*|fall\w*|ris\w*|drop\w*|pump\w*|dump\w*|surg\w*|rally\w*)\b",
            r"\b(here's why|this is why|reasons why)\b",
            r"\$\d+k?\b.*\b(possible|likely|incoming|imminent|soon)\b",
            r"\b(mooning|to the moon|next stop|eyeing)\b.*\$\d+",
            r"\b(price action|daily close|weekly close)\b",
            r"\b(target|targets|targeting)\b.*\$\d+",
        ],
    },
    {
        "category": "opinion",
        "causal_capable": False,
        "patterns": [
            r"\b(opinion|editorial|column|commentary|op-ed|take|perspective|viewpoint)\b",
            r"\b(i think|we believe|in my view|it seems|arguably|undoubtedly)\b",
            r"\b(should you|is it time|is it worth|should i|why i|how i)\b",
            r"\b(explained|explainer|guide to|beginner's guide)\b",
            r"\b(vs\.?|versus)\b.*\b(which|better|comparison)\b",
        ],
    },
    {
        "category": "promotion",
        "causal_capable": False,
        "patterns": [
            r"\b(airdrop|giveaway|presale|ico|ieo|ido|launchpad)\b",
            r"\b(memecoin|meme coin|shib|pepe|bonk|wif|floki)\b",
            r"\b(100x|1000x|10000x|\dx gains?|massive gains?)\b",
            r"\b(best crypto|top crypto|crypto to buy|next big|hidden gem|hidden gems)\b",
            r"\b(top \d+|best \d+|\d+ altcoins? to|\d+ coins to)\b.*\b(buy|watch|hold|invest|gain)\b",
            r"\b(sponsored|partner|ad|advertisement|promoted)\b",
            r"\b(new memecoin|trending memecoin|rocket|gem alert)\b",
            r"\b(gains? of \d+%|pumped \d+%|up \d\d\d+%|gains \d+%)\b",
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
