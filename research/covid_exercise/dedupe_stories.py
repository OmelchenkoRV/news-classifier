"""
Story Deduplication
====================

Computes a normalized 'story hash' for each headline so wire service
syndication of a single article doesn't inflate frequency counts.

The problem: AP wire publishes "Mystery pneumonia in China" → 47 local
papers reprint it → raw headline count says 47 events. Actually 1 event.

The hash:
  - Lowercase the title
  - Strip punctuation, numbers
  - Remove common stopwords
  - Sort remaining tokens alphabetically
  - Take SHA1 of joined tokens

So "Mystery pneumonia outbreak in China" and "Mystery pneumonia outbreak,
China sparks SARS fears" produce different hashes (different content),
but "China pneumonia outbreak mystery" reordered would match.

Why sorted-token hashing instead of fuzzy matching:
  - Deterministic, fast, no false positives on similar-but-distinct stories
  - Catches reordered headlines and minor punctuation changes
  - Doesn't merge "China outbreak" with "Australia outbreak" (different content)

What this DOES NOT do:
  - Doesn't merge paraphrases ("Wuhan pneumonia probe" vs "China pneumonia
    investigation" hash differently). That's a feature — different angles
    are arguably different stories.
  - Doesn't merge across languages.

Usage:
    # Add column + populate hashes
    python -m research.covid_exercise.dedupe_stories

    # Then re-run simulation - it picks up the new column automatically
    python -m research.covid_exercise.simulate \\
        --topic covid --bdba-topic pandemic_signal \\
        --start 2019-12-01 --end 2020-04-30
"""

import os
import sys
import re
import hashlib
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# Stopwords to strip before hashing — common English words that don't
# carry story-identifying meaning. We DO keep numbers stripped because
# "30 quarantined" vs "31 quarantined" should hash the same (rounding).
STOPWORDS = {
    "a", "an", "and", "or", "but", "the", "is", "are", "was", "were",
    "be", "been", "being", "to", "of", "in", "on", "at", "by", "for",
    "with", "from", "as", "this", "that", "these", "those", "it",
    "its", "if", "then", "than", "so", "such", "no", "not", "only",
    "own", "same", "very", "just", "now", "also", "new", "after",
    "before", "amid", "into", "over", "under", "about", "above",
    "below", "between", "through", "during", "since", "until",
    "says", "said", "say", "told", "according", "report", "reports",
    "reported", "reportedly", "may", "might", "could", "should",
    "would", "will", "can", "has", "have", "had", "do", "does",
    "did", "but", "all", "any", "each", "more", "most", "other",
    "some", "what", "which", "who", "when", "where", "why", "how",
    # Headline-specific noise
    "news", "update", "updates", "breaking", "exclusive",
}


def normalize_for_hash(title: str) -> str:
    """
    Reduce a title to a canonical form for duplicate detection.
    
    Steps:
      1. Lowercase
      2. Strip punctuation (replace with space)
      3. Strip numbers
      4. Tokenize on whitespace
      5. Remove stopwords and short tokens
      6. Sort tokens alphabetically
      7. Return joined string
    """
    # 1-3: lowercase, strip punctuation and digits
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\d+", " ", t)
    
    # 4-5: tokenize, filter
    tokens = [
        tok for tok in t.split()
        if len(tok) >= 3 and tok not in STOPWORDS
    ]
    
    # 6-7: sort and join
    tokens.sort()
    return " ".join(tokens)


def compute_hash(title: str) -> str:
    """SHA1 hex of the normalized title — short enough to index."""
    normalized = normalize_for_hash(title)
    if not normalized:
        # Empty after normalization - hash original lowercase
        normalized = title.lower().strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def add_column_if_needed(cur):
    """Add story_hash column to historical_headlines if it doesn't exist."""
    cur.execute("""
        ALTER TABLE historical_headlines
        ADD COLUMN IF NOT EXISTS story_hash TEXT
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_hh_story_hash
        ON historical_headlines(story_hash)
    """)


def populate_hashes():
    """Compute story_hash for all rows that don't have one yet."""
    conn = get_connection()
    cur = get_cursor(conn)
    
    add_column_if_needed(cur)
    conn.commit()
    
    # Pull all rows missing a hash
    cur.execute("""
        SELECT id, title FROM historical_headlines
        WHERE story_hash IS NULL
    """)
    rows = cur.fetchall()
    
    log.info(f"Computing story hashes for {len(rows):,} headlines...")
    
    update_cur = conn.cursor()
    for i, row in enumerate(rows):
        h = compute_hash(row["title"])
        update_cur.execute(
            "UPDATE historical_headlines SET story_hash = %s WHERE id = %s",
            (h, row["id"])
        )
        if (i + 1) % 5000 == 0:
            conn.commit()
            log.info(f"  ... {i+1:,}/{len(rows):,}")
    
    conn.commit()
    conn.close()
    log.info(f"Done. {len(rows):,} hashes populated.")


def show_dedup_stats():
    """Report how much duplication exists in the data."""
    conn = get_connection()
    cur = get_cursor(conn)
    
    cur.execute("""
        SELECT
            research_topic,
            COUNT(*) AS total_headlines,
            COUNT(DISTINCT story_hash) AS unique_stories,
            ROUND(100.0 * COUNT(DISTINCT story_hash) / COUNT(*), 1) AS unique_pct,
            ROUND(COUNT(*)::numeric / COUNT(DISTINCT story_hash), 1) AS avg_copies_per_story
        FROM historical_headlines
        WHERE story_hash IS NOT NULL
        GROUP BY research_topic
        ORDER BY research_topic
    """)
    
    print(f"\n{'Topic':<15s} {'Total':>10s} {'Unique':>10s} {'Unique%':>10s} {'Avg copies':>12s}")
    print("-" * 65)
    for r in cur.fetchall():
        print(f"{r['research_topic']:<15s} "
              f"{r['total_headlines']:>10,} "
              f"{r['unique_stories']:>10,} "
              f"{r['unique_pct']:>9}% "
              f"{r['avg_copies_per_story']:>12}")
    
    # Show worst-syndicated stories
    print()
    print("Top 10 most-syndicated stories (potential noise):")
    cur.execute("""
        SELECT 
            story_hash,
            COUNT(*) AS copies,
            MIN(title) AS sample_title,
            COUNT(DISTINCT source_domain) AS unique_domains
        FROM historical_headlines
        WHERE story_hash IS NOT NULL
        GROUP BY story_hash
        ORDER BY copies DESC
        LIMIT 10
    """)
    for r in cur.fetchall():
        title = r["sample_title"][:80]
        print(f"  {r['copies']:>4} copies / {r['unique_domains']:>3} domains: {title}")
    
    conn.close()


def main():
    populate_hashes()
    show_dedup_stats()


if __name__ == "__main__":
    main()
