"""
Manual Review Tool for High-Impact Headlines
==============================================

Simple CLI that shows each high-impact headline alongside the price
chart context, and asks you to assign a category.

Categories:
  g = geopolitical    (war, ceasefire, sanctions, tariffs, elections)
  r = regulatory      (SEC, bans, approvals, ETF decisions, legislation)
  e = exchange        (hack, insolvency, withdrawal halt, delisting)
  m = macro           (fed rates, CPI, recession, employment, oil)
  p = protocol        (upgrade, exploit, vulnerability, fork, merge)
  k = market_structure (liquidation cascade, whale move, ETF flow, stablecoin depeg)
  n = noise           (price prediction, opinion, promotion, irrelevant)
  s = skip            (can't determine, review later)

Usage:
    python -m labelling.review_tool
    python -m labelling.review_tool --re-review   (review already-reviewed ones again)
"""

import os
import sys
import argparse
import logging
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CATEGORIES = {
    "g": "geopolitical",
    "r": "regulatory",
    "e": "exchange",
    "m": "macro",
    "p": "protocol",
    "k": "market_structure",
    "n": "noise",
}

CATEGORY_DESCRIPTIONS = {
    "g": "geopolitical    — war, ceasefire, sanctions, tariffs, elections",
    "r": "regulatory      — SEC, bans, approvals, ETF, legislation",
    "e": "exchange        — hack, insolvency, withdrawal halt, delisting",
    "m": "macro           — fed rates, CPI, recession, employment",
    "p": "protocol        — upgrade, exploit, vulnerability, fork",
    "k": "market_structure — liquidation cascade, whale move, ETF flow, depeg",
    "n": "noise           — price prediction, opinion, promotion",
}


def get_price_context(cur, published_at, symbol="BTCUSDT"):
    """Get a mini price chart around the headline timestamp."""
    cur.execute("""
        SELECT timestamp, close
        FROM price_snapshots
        WHERE symbol = %s
          AND timestamp BETWEEN %s - INTERVAL '6 hours'
                             AND %s + INTERVAL '48 hours'
        ORDER BY timestamp
    """, (symbol, published_at, published_at))
    rows = cur.fetchall()
    if not rows:
        return None

    prices = [(r["timestamp"], float(r["close"])) for r in rows]
    pub_price = None
    for ts, price in prices:
        if ts >= published_at:
            pub_price = price
            break
    if not pub_price:
        pub_price = prices[0][1]

    return prices, pub_price


def render_mini_chart(prices, pub_time, pub_price, width=60, height=10):
    """Render a simple ASCII price chart."""
    if not prices or len(prices) < 2:
        return "  [No price data available]"

    closes = [p[1] for p in prices]
    min_p = min(closes)
    max_p = max(closes)
    spread = max_p - min_p
    if spread == 0:
        spread = 1

    lines = []
    # Simple bar chart
    step = max(1, len(closes) // width)
    sampled = closes[::step][:width]

    for row in range(height, 0, -1):
        threshold = min_p + (spread * row / height)
        line = "  "
        for val in sampled:
            if val >= threshold:
                line += "█"
            else:
                line += " "
        price_label = f" ${threshold:,.0f}" if row in (1, height // 2, height) else ""
        lines.append(line + price_label)

    # Time axis
    lines.append("  " + "─" * len(sampled))
    lines.append(f"  -6h{'':>{len(sampled)//2 - 5}}pub{'':>{len(sampled)//2 - 5}}+48h")

    return "\n".join(lines)


def review(re_review: bool = False):
    """Interactive review of high-impact headlines."""
    conn = get_connection()
    cur = get_cursor(conn)

    # Get headlines to review
    if re_review:
        query = """
            SELECT h.id as headline_id, h.title, h.source_name, h.published_at,
                   h.body_snippet, h.url,
                   l.impact_level, l.impact_direction,
                   l.price_move_1h, l.price_move_4h, l.price_move_24h, l.price_move_48h,
                   l.category
            FROM headline_labels l
            JOIN headlines h ON h.id = l.headline_id
            WHERE l.impact_level IN ('high', 'medium')
            ORDER BY ABS(l.price_move_48h) DESC
        """
    else:
        query = """
            SELECT h.id as headline_id, h.title, h.source_name, h.published_at,
                   h.body_snippet, h.url,
                   l.impact_level, l.impact_direction,
                   l.price_move_1h, l.price_move_4h, l.price_move_24h, l.price_move_48h,
                   l.category
            FROM headline_labels l
            JOIN headlines h ON h.id = l.headline_id
            WHERE l.impact_level IN ('high', 'medium')
              AND l.manually_reviewed = FALSE
            ORDER BY
                CASE l.impact_level
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                END,
                ABS(l.price_move_48h) DESC
        """

    cur.execute(query)
    headlines = cur.fetchall()

    if not headlines:
        print("\nNo headlines to review. Run auto_label first.")
        return

    print(f"\n{'='*70}")
    print(f"  HEADLINE REVIEW TOOL — {len(headlines)} headlines to categorize")
    print(f"{'='*70}")
    print()
    for key, desc in CATEGORY_DESCRIPTIONS.items():
        print(f"  [{key}] {desc}")
    print(f"  [s] skip    — can't determine, review later")
    print(f"  [q] quit    — save progress and exit")
    print(f"{'='*70}\n")

    reviewed = 0
    for i, h in enumerate(headlines):
        # Display headline context
        print(f"\n{'─'*70}")
        print(f"  [{i+1}/{len(headlines)}]  {h['impact_level'].upper()} {h['impact_direction'].upper()}")
        print(f"  Source: {h['source_name']}  |  {h['published_at'].strftime('%Y-%m-%d %H:%M UTC')}")
        if h['category']:
            print(f"  Current category: {h['category']}")
        print()
        print(f"  {h['title']}")
        print()
        if h['body_snippet']:
            snippet = h['body_snippet'][:200]
            print(f"  {snippet}{'...' if len(h['body_snippet']) > 200 else ''}")
            print()

        # Price moves
        moves = []
        for label, key in [("1h", "price_move_1h"), ("4h", "price_move_4h"),
                           ("24h", "price_move_24h"), ("48h", "price_move_48h")]:
            val = h.get(key)
            if val is not None:
                moves.append(f"{label}: {val:+.1%}")
        print(f"  Price moves (BTC):  {' | '.join(moves)}")

        # Mini chart
        prices_data = get_price_context(cur, h["published_at"])
        if prices_data:
            prices, pub_price = prices_data
            chart = render_mini_chart(prices, h["published_at"], pub_price)
            print()
            print(chart)

        # Get user input
        print()
        while True:
            choice = input("  Category [g/r/e/m/p/k/n/s/q]: ").strip().lower()
            if choice == "q":
                print(f"\n  Saved {reviewed} reviews. Exiting.")
                conn.close()
                return
            if choice == "s":
                break
            if choice in CATEGORIES:
                category = CATEGORIES[choice]
                cur.execute("""
                    UPDATE headline_labels
                    SET category = %s,
                        manually_reviewed = TRUE,
                        reviewed_at = NOW()
                    WHERE headline_id = %s
                """, (category, h["headline_id"]))
                conn.commit()
                reviewed += 1
                print(f"  -> {category}")
                break
            print("  Invalid choice. Try again.")

        # Progress save every 20 reviews
        if reviewed > 0 and reviewed % 20 == 0:
            conn.commit()
            print(f"\n  [Auto-saved {reviewed} reviews]")

    conn.commit()
    conn.close()
    print(f"\n{'='*70}")
    print(f"  Review complete: {reviewed} headlines categorized")
    print(f"{'='*70}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manually review high-impact headlines")
    parser.add_argument("--re-review", action="store_true", help="Review already-reviewed headlines again")
    args = parser.parse_args()

    review(re_review=args.re_review)
