"""
Auto-Label Headlines by Price Impact
======================================

For each headline, finds the BTC/ETH price at publication time, then
measures the maximum absolute price move at +1h, +4h, +24h, +48h.

Labels:
  impact_level:     'high' (>5%), 'medium' (2-5%), 'low' (0.5-2%), 'noise' (<0.5%)
  impact_direction: 'bullish' (moved up), 'bearish' (moved down), 'neutral'

Usage:
    python -m labelling.auto_label
    python -m labelling.auto_label --limit 1000
"""

import os
import sys
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def get_price_at(cur, symbol: str, target_ts, tolerance_minutes: int = 30):
    """Find the closest price snapshot to a target timestamp."""
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


def compute_max_move(cur, symbol: str, base_ts, base_price: float, hours: int):
    """
    Compute the max absolute % move in the next N hours after base_ts.
    Returns (max_move_pct, direction).
    """
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

    min_low = float(row["min_low"])
    max_high = float(row["max_high"])

    up_move = (max_high - base_price) / base_price
    down_move = (base_price - min_low) / base_price

    if abs(up_move) >= abs(down_move):
        return up_move, "bullish"
    else:
        return -down_move, "bearish"


def classify_impact(max_48h_move: float) -> str:
    """Classify the impact level based on max absolute move."""
    abs_move = abs(max_48h_move) if max_48h_move else 0
    if abs_move >= 0.05:
        return "high"
    elif abs_move >= 0.02:
        return "medium"
    elif abs_move >= 0.005:
        return "low"
    return "noise"


def auto_label(limit: int = None, symbol: str = "BTCUSDT"):
    """
    Auto-label all unlabelled headlines by cross-referencing with price data.
    """
    conn = get_connection()
    cur = get_cursor(conn)

    # Find headlines that don't have labels yet
    query = """
        SELECT h.id, h.title, h.published_at
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

    for i, h in enumerate(headlines):
        headline_id = h["id"]
        pub_ts = h["published_at"]

        # Get price at publication time
        base_price = get_price_at(cur, symbol, pub_ts, tolerance_minutes=60)
        if base_price is None:
            skipped += 1
            continue

        # Compute moves at each window
        moves = {}
        direction = "neutral"
        for hours in [1, 4, 24, 48]:
            move_pct, move_dir = compute_max_move(cur, symbol, pub_ts, base_price, hours)
            moves[hours] = move_pct
            if hours == 48 and move_dir:
                direction = move_dir

        max_48h = moves.get(48)
        impact = classify_impact(max_48h)

        try:
            cur.execute("""
                INSERT INTO headline_labels
                    (headline_id, price_move_1h, price_move_4h,
                     price_move_24h, price_move_48h,
                     impact_level, impact_direction)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (headline_id) DO UPDATE SET
                    price_move_1h = EXCLUDED.price_move_1h,
                    price_move_4h = EXCLUDED.price_move_4h,
                    price_move_24h = EXCLUDED.price_move_24h,
                    price_move_48h = EXCLUDED.price_move_48h,
                    impact_level = EXCLUDED.impact_level,
                    impact_direction = EXCLUDED.impact_direction
            """, (
                headline_id,
                moves.get(1), moves.get(4),
                moves.get(24), moves.get(48),
                impact, direction,
            ))
            labelled += 1
        except Exception as e:
            logger.warning(f"Label insert error for headline {headline_id}: {e}")
            conn.rollback()

        if (i + 1) % 500 == 0:
            conn.commit()
            logger.info(f"  Progress: {i+1}/{len(headlines)}, labelled: {labelled}, skipped: {skipped}")

    conn.commit()

    # Print distribution
    cur.execute("""
        SELECT impact_level, COUNT(*) as cnt
        FROM headline_labels
        GROUP BY impact_level
        ORDER BY cnt DESC
    """)
    logger.info(f"\nLabelling complete: {labelled} labelled, {skipped} skipped (no price data)")
    logger.info("Impact distribution:")
    for row in cur.fetchall():
        logger.info(f"  {row['impact_level']:10s}: {row['cnt']:>6d}")

    # Show high-impact headline examples
    cur.execute("""
        SELECT h.title, l.impact_level, l.impact_direction,
               l.price_move_1h, l.price_move_48h, h.published_at
        FROM headline_labels l
        JOIN headlines h ON h.id = l.headline_id
        WHERE l.impact_level = 'high'
        ORDER BY ABS(l.price_move_48h) DESC
        LIMIT 10
    """)
    logger.info("\nTop 10 highest-impact headlines:")
    for row in cur.fetchall():
        move = row["price_move_48h"]
        logger.info(
            f"  [{row['published_at'].strftime('%Y-%m-%d')}] "
            f"{row['impact_direction']:7s} {move:+.1%} 48h | "
            f"{row['title'][:80]}"
        )

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-label headlines by price impact")
    parser.add_argument("--limit", type=int, help="Max headlines to label")
    parser.add_argument("--symbol", type=str, default="BTCUSDT", help="Price symbol to use")
    args = parser.parse_args()

    auto_label(limit=args.limit, symbol=args.symbol)
