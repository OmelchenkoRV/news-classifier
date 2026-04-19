"""
Curated RSS feed sources for the news classifier.

These are the ~35 highest-quality, English-language, currently-active crypto
news sources. Prioritized by:
  - Tier 1: Major outlets that move markets (CoinDesk, CoinTelegraph, Bloomberg)
  - Tier 2: Solid crypto-native outlets with good signal
  - Tier 3: Niche but useful for specific categories

Sources extracted from:
  - github.com/nirholas/cryptocurrency.cv (200+ sources, filtered)
  - github.com/mclassy/Cryptocurrency-RSS-Feed-List
  - github.com/kukapay/crypto-rss-mcp (Chainfeeds OPML)
  - Manual verification of feed availability (April 2026)

Run seed_rss_sources() to insert into database.
"""

# Each entry: (name, url, category, tier)
# Categories: general, bitcoin, ethereum, defi, regulation, macro, exchange, research
# Tiers: 1=market-moving major, 2=solid crypto-native, 3=niche/supplementary

RSS_FEEDS = [
    # ══════════════════════════════════════════════════════════════
    # TIER 1 — Major outlets, market-moving headlines
    # ══════════════════════════════════════════════════════════════
    ("CoinDesk",            "https://www.coindesk.com/arc/outboundfeeds/rss/",         "general",    1),
    ("CoinTelegraph",       "https://cointelegraph.com/rss",                            "general",    1),
    ("Decrypt",             "https://decrypt.co/feed",                                  "general",    1),
    ("DL News",             "https://www.dlnews.com/arc/outboundfeeds/rss/",           "general",    1),
    ("Bloomberg Crypto",    "https://feeds.bloomberg.com/crypto/news.rss",              "macro",      1),
    # The Block: RSS broken as of Apr 2026 — removed

    # ══════════════════════════════════════════════════════════════
    # TIER 2 — Solid crypto-native sources
    # ══════════════════════════════════════════════════════════════
    ("Bitcoin Magazine",    "https://bitcoinmagazine.com/feed",                         "bitcoin",    2),
    ("Blockworks",          "https://blockworks.co/feed",                               "general",    2),
    ("Unchained",           "https://unchainedcrypto.com/feed/",                        "general",    2),
    ("The Defiant",         "https://thedefiant.io/feed",                               "defi",       2),
    ("CryptoSlate",         "https://cryptoslate.com/feed/",                            "general",    2),
    ("Bitcoinist",          "https://bitcoinist.com/feed/",                             "bitcoin",    2),
    ("NewsBTC",             "https://www.newsbtc.com/feed/",                            "general",    2),
    ("CryptoPotato",        "https://cryptopotato.com/feed/",                           "general",    2),
    ("BeInCrypto",          "https://beincrypto.com/feed/",                             "general",    2),
    ("Crypto Briefing",     "https://cryptobriefing.com/feed/",                         "general",    2),
    ("U.Today",             "https://u.today/rss",                                      "general",    2),
    ("AMBCrypto",           "https://ambcrypto.com/feed/",                              "general",    2),
    ("CoinGape",            "https://coingape.com/feed/",                               "general",    2),
    ("Protos",              "https://protos.com/feed/",                                 "general",    2),
    ("WuBlockchain",        "https://wuBlockchain.substack.com/feed",                   "general",    2),
    # Bankless, DeFi Pulse, Etherscan: RSS broken as of Apr 2026 — removed

    # ══════════════════════════════════════════════════════════════
    # TIER 2 — Regulation / Macro
    # ══════════════════════════════════════════════════════════════
    ("CoinCenter",          "https://www.coincenter.org/feed/",                         "regulation", 2),

    # ══════════════════════════════════════════════════════════════
    # TIER 3 — Niche but useful
    # ══════════════════════════════════════════════════════════════
    ("Glassnode Insights",  "https://insights.glassnode.com/rss/",                     "research",   3),
    ("Bitcoin Core",        "https://bitcoincore.org/en/rss.xml",                       "bitcoin",    3),
    # Messari, Delphi, Rekt, CryptoQuant, Reuters, CNBC, Forbes:
    # broken or empty as of Apr 2026 — removed
]


def seed_rss_sources():
    """Insert curated RSS sources into the database."""
    from config.database import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            inserted = 0
            skipped = 0
            for name, url, category, tier in RSS_FEEDS:
                try:
                    cur.execute("""
                        INSERT INTO rss_sources (name, url, category, tier)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (url) DO UPDATE SET
                            name = EXCLUDED.name,
                            category = EXCLUDED.category,
                            tier = EXCLUDED.tier,
                            is_active = TRUE
                    """, (name, url, category, tier))
                    if cur.rowcount > 0:
                        inserted += 1
                    else:
                        skipped += 1
                except Exception as e:
                    print(f"  Error inserting {name}: {e}")
                    skipped += 1

            conn.commit()
            print(f"RSS sources seeded: {inserted} inserted/updated, {skipped} skipped")
            print(f"Total feeds: {len(RSS_FEEDS)}")

            # Print summary by tier
            cur.execute("""
                SELECT tier, COUNT(*) as cnt, 
                       array_agg(name ORDER BY name) as names
                FROM rss_sources 
                WHERE is_active = TRUE
                GROUP BY tier ORDER BY tier
            """)
            for row in cur.fetchall():
                print(f"  Tier {row[0]}: {row[1]} feeds")
    finally:
        conn.close()


if __name__ == "__main__":
    seed_rss_sources()
