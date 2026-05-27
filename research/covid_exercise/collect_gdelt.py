"""
GDELT Historical News Collector
================================

Pulls news headlines matching BDBA topic keywords from GDELT's DOC API
for a specified time window.

GDELT API:
  https://api.gdeltproject.org/api/v2/doc/doc

Notes:
  - GDELT covers from 2015 onwards in the v2 API (we need this for COVID 2019-20)
  - Free tier rate limits exist - we throttle with small delays
  - Each query returns up to 250 results per page
  - We chunk by week to stay within result caps
  - GDELT returns: title, url, source country, source domain, published date

The collector tags each headline with the research_topic ('covid', 
'ebola_2014', 'mers_2015', 'uap_2017') based on which window we're pulling.

Usage:
    # Pull the COVID warm-up window
    python -m research.covid_exercise.collect_gdelt \\
        --topic covid \\
        --keywords-from pandemic_signal \\
        --start 2019-11-01 --end 2020-04-30
    
    # Control test: Ebola 2014
    python -m research.covid_exercise.collect_gdelt \\
        --topic ebola_2014 \\
        --keywords-from pandemic_signal \\
        --start 2014-07-01 --end 2014-12-31
"""

import os
import sys
import time
import json
import argparse
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Throttle between requests. GDELT free tier is unforgiving — go slowly.
REQUEST_DELAY_SEC = 4.0

# Chunk size for date range walking. Larger = fewer requests but risk
# exceeding 250-result cap on dense periods. 14 days is a good balance.
CHUNK_DAYS = 14

# Tier1 domains — used for source diversification analysis
# These are the mainstream outlets whose coverage signals "this is now
# a serious story, not a fringe report"
TIER1_DOMAINS = {
    "reuters.com", "bloomberg.com", "wsj.com", "nytimes.com",
    "ft.com", "economist.com", "washingtonpost.com",
    "ap.org", "apnews.com", "bbc.com", "bbc.co.uk",
    "cnn.com", "nbcnews.com", "cbsnews.com", "abcnews.go.com",
    "theguardian.com", "telegraph.co.uk",
    "nikkei.com", "scmp.com",
}

TIER2_DOMAINS = {
    "cnbc.com", "marketwatch.com", "businessinsider.com",
    "forbes.com", "fortune.com", "axios.com",
    "politico.com", "thehill.com",
    "independent.co.uk", "thetimes.co.uk",
    "lemonde.fr", "spiegel.de", "elpais.com",
    "japantimes.co.jp", "straitstimes.com",
}


def classify_tier(domain: str) -> str:
    """Classify a source domain into tiers."""
    if not domain:
        return "tier3"
    domain = domain.lower().lstrip("www.")
    if domain in TIER1_DOMAINS:
        return "tier1"
    if domain in TIER2_DOMAINS:
        return "tier2"
    return "tier3"


def get_topic_keywords(topic_name: str) -> tuple[list, list, list]:
    """Fetch a topic's keyword config from the database."""
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT primary_keywords, boost_keywords, exclude_keywords
        FROM bdba_topics WHERE name = %s
    """, (topic_name,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Topic not found in bdba_topics: {topic_name}")
    return (
        row["primary_keywords"],
        row["boost_keywords"] or [],
        row["exclude_keywords"] or [],
    )


def build_gdelt_query(keywords: list[str]) -> str:
    """
    Build a GDELT search query from a list of keywords.
    
    GDELT supports OR (uppercase) and quoted phrases.
    Single keyword: just the quoted phrase, no parens.
    Multi-keyword: OR'd inside parens.
    
    GDELT REJECTS '("foo")' with "Parentheses may only be used around 
    OR'd statements" — so single keywords must NOT be wrapped.
    """
    if not keywords:
        return ""
    
    quoted = [f'"{kw}"' for kw in keywords]
    
    if len(quoted) == 1:
        # Single keyword - no parens
        query = quoted[0]
    else:
        # Multi-keyword OR query
        query = "(" + " OR ".join(quoted) + ")"
    
    if len(query) > 250:
        log.warning(f"Query length {len(query)} exceeds 250 char GDELT limit. "
                    f"Will split into batches.")
    return query


def split_keywords_into_batches(keywords: list[str], max_query_chars: int = 200) -> list[list[str]]:
    """Split keywords so each resulting OR query fits under GDELT's limit."""
    batches = []
    current = []
    current_len = 2  # parens
    for kw in keywords:
        kw_quoted = f'"{kw}"'
        added_len = len(kw_quoted) + len(" OR ")
        if current_len + added_len > max_query_chars and current:
            batches.append(current)
            current = [kw]
            current_len = 2 + len(kw_quoted)
        else:
            current.append(kw)
            current_len += added_len
    if current:
        batches.append(current)
    return batches


def parse_articles_lenient(text: str) -> list[dict]:
    """
    Try to extract articles from possibly-truncated JSON.
    
    GDELT sometimes returns valid JSON that gets cut off mid-stream.
    Strict json.loads() fails, but we can salvage articles by:
      1. Trying full parse first (the happy path)
      2. If that fails, finding the articles array and parsing each
         {} object individually until we hit a malformed one
    
    Returns list of articles successfully parsed (possibly empty,
    possibly truncated relative to what GDELT intended to return).
    """
    text = text.strip()
    
    # Happy path
    try:
        data = json.loads(text)
        return data.get("articles", [])
    except json.JSONDecodeError:
        pass
    
    # Salvage path: find "articles": [ ... and parse objects one at a time
    articles_marker = '"articles":'
    idx = text.find(articles_marker)
    if idx == -1:
        return []
    
    # Find the opening [
    bracket_idx = text.find("[", idx)
    if bracket_idx == -1:
        return []
    
    # Walk through the array, parsing balanced {} objects
    pos = bracket_idx + 1
    articles = []
    depth = 0
    obj_start = -1
    in_string = False
    escape_next = False
    
    while pos < len(text):
        c = text[pos]
        
        if escape_next:
            escape_next = False
            pos += 1
            continue
        
        if in_string:
            if c == '\\':
                escape_next = True
            elif c == '"':
                in_string = False
            pos += 1
            continue
        
        if c == '"':
            in_string = True
            pos += 1
            continue
        
        if c == '{':
            if depth == 0:
                obj_start = pos
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and obj_start != -1:
                # Try to parse this object
                obj_text = text[obj_start:pos+1]
                try:
                    article = json.loads(obj_text)
                    articles.append(article)
                except json.JSONDecodeError:
                    # Malformed object - stop salvaging
                    break
                obj_start = -1
        elif c == ']' and depth == 0:
            # Clean end of array
            break
        
        pos += 1
    
    return articles


def fetch_gdelt_chunk(query: str, start: datetime, end: datetime,
                     max_records: int = 250, max_retries: int = 4) -> list[dict]:
    """
    Pull one chunk of GDELT results, with retry on timeout/rate-limit.
    
    GDELT date format: YYYYMMDDHHMMSS (UTC)
    Backoff: 5s, 15s, 45s, 120s before giving up.
    
    Important: GDELT often degrades silently — returns HTTP 200 with empty
    body, error string, or truncated JSON when rate-limited or under load.
    We:
      - Treat empty / error-string responses as retryable
      - Use lenient JSON parser to salvage articles from truncated responses
    """
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        "sort": "datedesc",
    }
    
    backoffs = [5, 15, 45, 120]
    for attempt in range(max_retries):
        try:
            r = requests.get(GDELT_DOC_URL, params=params, timeout=60)
            
            # Handle rate limiting explicitly
            if r.status_code == 429:
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                log.warning(f"GDELT rate limited (429), waiting {wait}s...")
                time.sleep(wait)
                continue
            
            r.raise_for_status()
            
            text = r.text.strip()
            if not text:
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                log.warning(f"GDELT returned empty body (likely rate-limited), "
                            f"waiting {wait}s...")
                time.sleep(wait)
                continue
            
            # Detect plain-text error responses (usually start with capital
            # letter and don't contain JSON markers)
            if not text.startswith("{") and not text.startswith("["):
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                log.warning(f"GDELT error response (likely rate-limited), "
                            f"waiting {wait}s... first 80 chars: {text[:80]!r}")
                time.sleep(wait)
                continue
            
            # Try lenient JSON parse (handles truncated responses)
            articles = parse_articles_lenient(text)
            if articles or text.endswith("}") or text.endswith("]"):
                # Got articles, OR the response is properly closed (just empty)
                return articles
            
            # JSON looked malformed and we got nothing — retry
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            log.warning(f"GDELT response could not be parsed at all, "
                        f"waiting {wait}s... first 80 chars: {text[:80]!r}")
            time.sleep(wait)
            continue
        
        except requests.exceptions.Timeout:
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            log.warning(f"GDELT timeout (attempt {attempt+1}/{max_retries}), "
                        f"waiting {wait}s...")
            time.sleep(wait)
        
        except requests.exceptions.ConnectionError as e:
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            log.warning(f"GDELT connection error (attempt {attempt+1}/{max_retries}): "
                        f"{type(e).__name__}, waiting {wait}s...")
            time.sleep(wait)
        
        except requests.exceptions.RequestException as e:
            log.error(f"GDELT request failed (non-retryable): {e}")
            return []
    
    log.error(f"GDELT fetch failed after {max_retries} retries, skipping chunk")
    return []


def collect_window(topic: str, keywords_from: str,
                   start: datetime, end: datetime):
    """
    Pull all GDELT results for a topic over a date window.
    
    Strategy:
      - Split keywords into query-size-safe batches
      - Walk forward in 7-day chunks (GDELT caps at 250 results per query)
      - Insert with ON CONFLICT to handle dupes across batches
    """
    primary, boost, exclude = get_topic_keywords(keywords_from)
    
    log.info(f"Topic: {topic}")
    log.info(f"Keywords source: {keywords_from}")
    log.info(f"Window: {start.date()} to {end.date()}")
    log.info(f"Primary keywords: {len(primary)}")
    
    all_keywords = primary + boost
    keyword_batches = split_keywords_into_batches(all_keywords)
    log.info(f"Keyword batches: {len(keyword_batches)}")
    for i, batch in enumerate(keyword_batches):
        log.info(f"  Batch {i+1}: {len(batch)} keywords")
    
    # Walk forward in chunks
    chunk_size = timedelta(days=CHUNK_DAYS)
    total_inserted = 0
    
    conn = get_connection()
    cur = get_cursor(conn)
    
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + chunk_size, end)
        
        for batch_idx, kw_batch in enumerate(keyword_batches):
            query = build_gdelt_query(kw_batch)
            
            log.info(f"Fetching {chunk_start.date()} to {chunk_end.date()} "
                     f"(batch {batch_idx+1}/{len(keyword_batches)})")
            
            articles = fetch_gdelt_chunk(query, chunk_start, chunk_end)
            log.info(f"  → {len(articles)} articles returned")
            
            for art in articles:
                title = art.get("title", "").strip()
                url = art.get("url", "").strip()
                domain = art.get("domain", "").lower()
                
                # Parse seendate: 20191215120000
                seendate = art.get("seendate")
                if not seendate:
                    continue
                try:
                    pub_dt = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    try:
                        pub_dt = datetime.strptime(seendate, "%Y%m%d%H%M%S").replace(
                            tzinfo=timezone.utc)
                    except ValueError:
                        continue
                
                if not title or not url:
                    continue
                
                # Apply exclude filter — skip if any exclude keyword present
                title_lower = title.lower()
                if any(ex.lower() in title_lower for ex in exclude):
                    continue
                
                # Find which keywords matched
                matched = [
                    kw for kw in all_keywords
                    if kw.lower() in title_lower
                ]
                if not matched:
                    # Headline returned but doesn't match in title
                    # (GDELT may have matched body text). Skip — we focus on titles.
                    continue
                
                tier = classify_tier(domain)
                
                try:
                    cur.execute("""
                        INSERT INTO historical_headlines
                            (source_type, source_domain, source_country,
                             title, url, published_at,
                             research_topic, keyword_matches, source_tier)
                        VALUES ('gdelt', %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (url, published_at) DO NOTHING
                    """, (
                        domain,
                        art.get("sourcecountry"),
                        title[:500],  # safety
                        url,
                        pub_dt,
                        topic,
                        matched,
                        tier,
                    ))
                    total_inserted += cur.rowcount
                except Exception as e:
                    log.warning(f"Insert failed for {url[:80]}: {e}")
            
            conn.commit()
            time.sleep(REQUEST_DELAY_SEC)
        
        chunk_start = chunk_end
    
    conn.close()
    log.info(f"Total new headlines inserted: {total_inserted}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True,
                        help="research_topic value (covid, ebola_2014, etc)")
    parser.add_argument("--keywords-from", required=True,
                        help="bdba_topics.name to pull keywords from")
    parser.add_argument("--start", required=True,
                        help="YYYY-MM-DD start date")
    parser.add_argument("--end", required=True,
                        help="YYYY-MM-DD end date")
    args = parser.parse_args()
    
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    
    collect_window(args.topic, args.keywords_from, start, end)


if __name__ == "__main__":
    main()
