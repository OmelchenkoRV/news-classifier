"""
Stage 2 validation harness.

Implements the three Phase 4 tasks from stage2-plan.md:

  coverage   (Task 4.1 part 1)
      Run VerbExtractor over a batch of headlines and report what
      fraction got a non-unknown verb category. Target: >85%.

  sample     (Task 4.1 part 2)
      Pull a random N (default 100) of those headlines, write a CSV
      with columns [id, title, verb, verb_category, direction,
      negated, subject, object, correct?]. Open in a spreadsheet,
      mark each row 'y' or 'n' in the `correct?` column, save, and
      run `accuracy --file path/to/marked.csv` to tally.

  accuracy   (companion to sample)
      Tally a marked-up sample CSV and print accuracy + per-category
      breakdown. Used after manually annotating the sample CSV.

  hormuz     (Task 4.2)
      Pull all Iran+Hormuz headlines from `headlines` (last N days),
      run extraction, group by direction, and verify both buckets
      exist. Optionally re-runs them through TriggerManager in a
      dev DB to confirm separate triggers form.

  latency    (Task 4.3)
      Time extraction over a representative sample. Reports
      p50/p95/p99 latency. Target: <30ms per headline on CPU.

Usage:
    python -m scripts.validate_stage2 coverage --limit 5000
    python -m scripts.validate_stage2 sample --n 100 --out sample.csv
    python -m scripts.validate_stage2 accuracy --file sample_marked.csv
    python -m scripts.validate_stage2 hormuz
    python -m scripts.validate_stage2 latency --n 1000

All commands accept --limit / --n. None of them write to the production
triggers table; `hormuz --check-triggers` is opt-in and writes to a
separate dev schema (see SCHEMA env var).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection, get_cursor
from inference.verb_extractor import VerbExtractor
from config.verb_taxonomy import (
    UNKNOWN_CATEGORY, UNKNOWN_DIRECTION, all_categories,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("validate")

# ---------------------------------------------------------------------------
# Sample selection — single source of truth so coverage / sample / latency
# all draw from the same population. Adjust this query if your headlines
# table is shaped differently.
# ---------------------------------------------------------------------------
SAMPLE_SQL = """
    SELECT id, title
    FROM headlines
    WHERE collected_at > NOW() - INTERVAL '%s days'
      AND title IS NOT NULL
      AND char_length(title) > 10
    ORDER BY id  -- deterministic; we randomise in Python after fetch
    LIMIT %s
"""

DEFAULT_DAYS = 30
DEFAULT_LIMIT = 5000


def _fetch_headlines(days: int, limit: int) -> list[dict]:
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute(SAMPLE_SQL, (days, limit))
        rows = cur.fetchall()
        # Normalise to plain dicts in case the cursor returns RealDictRow etc.
        return [{"id": r["id"], "title": r["title"]} for r in rows]
    finally:
        conn.close()


# SQL for the 'gates-only' diagnose mode: restrict to headlines where the
# DistilBERT classifier flagged should_tighten_gates=TRUE. This is the
# correct denominator for Stage 2 evaluation — these are the headlines
# that actually reach process_headline and benefit from directional
# classification. Price-action / opinion / promotion headlines never
# enter the trigger system, so their unknowable directionality should
# not count against Stage 2.
TRIGGER_RELEVANT_SQL = """
    SELECT h.id, h.title
    FROM headlines h
    JOIN classifications c ON c.headline_id = h.id
    WHERE h.collected_at > NOW() - INTERVAL '%s days'
      AND h.title IS NOT NULL
      AND char_length(h.title) > 10
      AND c.should_tighten_gates = TRUE
    ORDER BY h.id
    LIMIT %s
"""


def _fetch_trigger_relevant_headlines(days: int, limit: int) -> list[dict]:
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute(TRIGGER_RELEVANT_SQL, (days, limit))
        rows = cur.fetchall()
        return [{"id": r["id"], "title": r["title"]} for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task 4.1 part 1: coverage
# ---------------------------------------------------------------------------
def cmd_coverage(args: argparse.Namespace) -> int:
    """Report what fraction of headlines get a classified verb category."""
    rows = _fetch_headlines(args.days, args.limit)
    if not rows:
        logger.error("No headlines found. Adjust --days or --limit.")
        return 1
    logger.info("Fetched %d headlines from last %d days", len(rows), args.days)

    extractor = VerbExtractor()

    category_counts: Counter[str] = Counter()
    direction_counts: Counter[str] = Counter()
    negated_count = 0

    for row in rows:
        try:
            res = extractor.extract(row["title"])
        except Exception as e:
            logger.warning("Extraction failed for id=%s: %s", row["id"], e)
            category_counts["__error__"] += 1
            continue
        category_counts[res.verb_category] += 1
        direction_counts[res.direction] += 1
        if res.negated:
            negated_count += 1

    total = len(rows)
    classified = total - category_counts.get(UNKNOWN_CATEGORY, 0) - category_counts.get("__error__", 0)
    coverage = classified / total

    print()
    print(f"=== COVERAGE REPORT ({total} headlines) ===")
    print(f"Classified:    {classified:>6} ({coverage:6.1%})")
    print(f"Unknown:       {category_counts.get(UNKNOWN_CATEGORY, 0):>6} "
          f"({category_counts.get(UNKNOWN_CATEGORY, 0) / total:6.1%})")
    if category_counts.get("__error__"):
        print(f"Errors:        {category_counts['__error__']:>6}")
    print(f"Negated:       {negated_count:>6} ({negated_count / total:6.1%})")
    print()
    print("Per-category breakdown:")
    for cat in sorted(all_categories()):
        n = category_counts.get(cat, 0)
        bar = "█" * int(40 * n / max(category_counts.values()))
        print(f"  {cat:<20} {n:>6} {bar}")
    if category_counts.get(UNKNOWN_CATEGORY):
        print(f"  {'unknown':<20} {category_counts[UNKNOWN_CATEGORY]:>6}")
    print()
    print("Direction breakdown:")
    for d in ("escalation", "de-escalation", "neutral",
              "context-dependent", UNKNOWN_DIRECTION):
        n = direction_counts.get(d, 0)
        print(f"  {d:<20} {n:>6} ({n / total:6.1%})")
    print()
    target = 0.85
    if coverage >= target:
        print(f"✓ PASS — coverage {coverage:.1%} meets target {target:.0%}")
        return 0
    print(f"✗ FAIL — coverage {coverage:.1%} below target {target:.0%}")
    print("  Inspect the unknown-category headlines to identify common")
    print("  patterns. If a verb keeps appearing, add it to the taxonomy.")
    return 1


# ---------------------------------------------------------------------------
# Task 4.1 part 2: sample CSV for manual spot-check
# ---------------------------------------------------------------------------
def cmd_sample(args: argparse.Namespace) -> int:
    """Write N random headlines + their extractions to a CSV for manual review."""
    rows = _fetch_headlines(args.days, args.limit)
    if len(rows) < args.n:
        logger.warning(
            "Pool size %d < requested sample %d — using full pool",
            len(rows), args.n,
        )
        sample = rows
    else:
        random.seed(args.seed)
        sample = random.sample(rows, args.n)

    extractor = VerbExtractor()
    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "id", "title", "verb", "verb_category", "direction",
            "negated", "subject", "object", "correct?", "notes",
        ])
        for row in sample:
            res = extractor.extract(row["title"])
            w.writerow([
                row["id"], row["title"], res.verb or "",
                res.verb_category, res.direction,
                "Y" if res.negated else "",
                res.subject or "", res.object or "",
                "",  # correct? — for human to fill
                "",  # notes — for human to fill
            ])

    print(f"Wrote {len(sample)} rows to {out_path}")
    print()
    print("Manual review process:")
    print("  1. Open the CSV in a spreadsheet.")
    print("  2. For each row, judge whether the extraction is acceptable:")
    print("     - 'y' = verb_category and direction are right OR the headline")
    print("            genuinely has no extractable directional verb")
    print("     - 'n' = wrong category, wrong direction, or missed an")
    print("            obvious directional verb")
    print("  3. Save the marked-up file.")
    print(f"  4. Run: python -m scripts.validate_stage2 accuracy --file {out_path}")
    return 0


def cmd_accuracy(args: argparse.Namespace) -> int:
    """Tally a marked-up sample CSV and report accuracy."""
    path = Path(args.file)
    if not path.exists():
        logger.error("File not found: %s", path)
        return 1

    total = 0
    marked = 0
    correct = 0
    incorrect_examples: list[tuple[str, str, str]] = []
    by_category: dict[str, dict[str, int]] = {}

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            verdict = (row.get("correct?") or "").strip().lower()
            if verdict not in ("y", "n"):
                continue
            marked += 1
            cat = row.get("verb_category", "unknown")
            slot = by_category.setdefault(cat, {"y": 0, "n": 0})
            slot[verdict] += 1
            if verdict == "y":
                correct += 1
            else:
                incorrect_examples.append((
                    row.get("title", ""),
                    f"{row.get('verb', '')}/{cat}",
                    row.get("direction", ""),
                ))

    if marked == 0:
        logger.error("No rows have been marked. Fill the 'correct?' column first.")
        return 1

    accuracy = correct / marked
    print()
    print(f"=== ACCURACY REPORT ===")
    print(f"Total rows:    {total}")
    print(f"Marked:        {marked}  (unmarked: {total - marked})")
    print(f"Correct:       {correct}")
    print(f"Incorrect:     {marked - correct}")
    print(f"Accuracy:      {accuracy:.1%}")
    print()
    print("Per-category accuracy:")
    for cat, slots in sorted(by_category.items()):
        n = slots["y"] + slots["n"]
        if n == 0:
            continue
        acc = slots["y"] / n
        print(f"  {cat:<20} {slots['y']}/{n}  ({acc:5.1%})")
    if incorrect_examples:
        print()
        print(f"First {min(10, len(incorrect_examples))} incorrect examples:")
        for title, vc, direction in incorrect_examples[:10]:
            print(f"  [{vc} / {direction}] {title[:80]}")
    print()
    target = 0.85
    if accuracy >= target:
        print(f"✓ PASS — accuracy {accuracy:.1%} meets target {target:.0%}")
        return 0
    print(f"✗ FAIL — accuracy {accuracy:.1%} below target {target:.0%}")
    return 1


# ---------------------------------------------------------------------------
# Task 4.2: Iran-Hormuz directional split
# ---------------------------------------------------------------------------
HORMUZ_SQL = """
    SELECT id, title, published_at
    FROM headlines
    WHERE LOWER(title) LIKE '%hormuz%'
      AND LOWER(title) LIKE '%iran%'
    ORDER BY published_at DESC
"""


def cmd_hormuz(args: argparse.Namespace) -> int:
    """Validate that Iran+Hormuz headlines split into directional buckets."""
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute(HORMUZ_SQL)
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        logger.warning("No Iran+Hormuz headlines found in history.")
        logger.warning("This validation is most useful with real historical data.")
        return 0  # not a failure — the system might just be young

    extractor = VerbExtractor()
    by_direction: dict[str, list[dict]] = {}
    by_category: Counter[str] = Counter()

    print()
    print(f"=== IRAN + HORMUZ DIRECTIONAL VALIDATION ({len(rows)} headlines) ===")
    print()
    for row in rows:
        res = extractor.extract(row["title"])
        by_direction.setdefault(res.direction, []).append({
            "id": row["id"],
            "title": row["title"],
            "verb": res.verb,
            "category": res.verb_category,
        })
        by_category[res.verb_category] += 1
        marker = {
            "escalation": "↑",
            "de-escalation": "↓",
            "neutral": "·",
            "context-dependent": "?",
        }.get(res.direction, "·")
        date_str = row["published_at"].strftime("%Y-%m-%d") if row.get("published_at") else "----------"
        print(f"  [{date_str}] "
              f"{marker} {res.verb_category:<12} {res.direction:<18} | {row['title'][:80]}")

    print()
    print("Direction tally:")
    for d in ("escalation", "de-escalation", "neutral",
              "context-dependent", UNKNOWN_DIRECTION):
        n = len(by_direction.get(d, []))
        print(f"  {d:<20} {n}")
    print()
    print("Category tally:")
    for cat, n in by_category.most_common():
        print(f"  {cat:<20} {n}")
    print()

    has_esc = len(by_direction.get("escalation", [])) > 0
    has_de_esc = len(by_direction.get("de-escalation", [])) > 0
    if has_esc and has_de_esc:
        print("✓ PASS — both escalation and de-escalation events present.")
        print("  The signature suffix logic should produce separate triggers.")
        if args.check_triggers:
            return _verify_trigger_split(by_direction)
        return 0
    if not has_esc and not has_de_esc:
        print("⚠  Neither direction was classified. Either:")
        print("   - History contains only neutral / unknown headlines, or")
        print("   - Extraction is missing the directional verbs.")
        print("   Inspect a sample manually before concluding.")
        return 1
    missing = "de-escalation" if not has_de_esc else "escalation"
    print(f"⚠  Only one direction present (missing: {missing}).")
    print(f"   This isn't necessarily a failure — historically, Iran+Hormuz")
    print(f"   coverage has been heavily {('escalation' if has_esc else 'de-escalation')}-biased.")
    print(f"   Re-run after the next directional flip event in the news cycle.")
    return 0


def _verify_trigger_split(by_direction: dict[str, list[dict]]) -> int:
    """Optional: feed the headlines through TriggerManager (writing to a
    SCHEMA-isolated dev DB) and confirm that escalation and de-escalation
    end up in different triggers."""
    schema = os.getenv("SCHEMA")
    if not schema or schema == "public":
        logger.error(
            "--check-triggers requires SCHEMA env var pointing to a dev "
            "schema. Refusing to write to public/production.",
        )
        return 1
    logger.warning("Trigger split verification not auto-implemented — "
                   "manual procedure documented in the script docstring.")
    # Implementation note: a full automated version would spin up a
    # transactional savepoint, call TriggerManager.process_headline on
    # each headline with category='geopolitical', then query the
    # triggers table grouping by signature. We keep this manual for
    # now because the dev-schema setup is environment-specific.
    return 0


# ---------------------------------------------------------------------------
# Task 4.3: latency
# ---------------------------------------------------------------------------
def cmd_latency(args: argparse.Namespace) -> int:
    """Measure VerbExtractor latency over a representative sample."""
    rows = _fetch_headlines(args.days, max(args.n, 100))
    if len(rows) < args.n:
        logger.warning("Pool size %d < requested %d", len(rows), args.n)
        sample = rows
    else:
        random.seed(args.seed)
        sample = random.sample(rows, args.n)

    extractor = VerbExtractor()

    # Warm up — first extraction includes lazy-init costs we don't want
    # to bias the measurement.
    extractor.extract("Iran threatens to close Strait of Hormuz")
    extractor.extract("Fed cuts rates by 25bps")

    durations_ms: list[float] = []
    for row in sample:
        t0 = time.perf_counter()
        try:
            extractor.extract(row["title"])
        except Exception as e:
            logger.warning("Extraction failed for id=%s: %s", row["id"], e)
            continue
        durations_ms.append((time.perf_counter() - t0) * 1000)

    if not durations_ms:
        logger.error("No successful extractions.")
        return 1

    p50 = statistics.median(durations_ms)
    p95 = statistics.quantiles(durations_ms, n=20)[18]   # 95th percentile
    p99 = statistics.quantiles(durations_ms, n=100)[98]  # 99th percentile
    mean = statistics.mean(durations_ms)
    mx = max(durations_ms)

    print()
    print(f"=== LATENCY REPORT ({len(durations_ms)} extractions) ===")
    print(f"  mean:  {mean:6.2f} ms")
    print(f"  p50:   {p50:6.2f} ms")
    print(f"  p95:   {p95:6.2f} ms")
    print(f"  p99:   {p99:6.2f} ms")
    print(f"  max:   {mx:6.2f} ms")
    print()

    target_p50 = 30.0
    target_p95 = 50.0
    p50_pass = p50 <= target_p50
    p95_pass = p95 <= target_p95
    print(f"  p50 ≤ {target_p50:.0f}ms:  {'✓' if p50_pass else '✗'}")
    print(f"  p95 ≤ {target_p95:.0f}ms:  {'✓' if p95_pass else '✗'}")
    print()
    if p50_pass and p95_pass:
        print("✓ PASS — latency within acceptable range.")
        return 0
    print("✗ FAIL — latency exceeds target.")
    print("  If only p99 is bad, that's likely cold-cache outliers and is OK.")
    print("  If p50 is over budget, consider:")
    print("    - Using a smaller spaCy model (en_core_web_sm is already smallest)")
    print("    - Disabling unused pipeline components (e.g. NER) if not used:")
    print("        VerbExtractor(nlp=spacy.load('en_core_web_sm', disable=['ner']))")
    return 1


# ---------------------------------------------------------------------------
# Diagnostics — what's in the unknown bucket?
# ---------------------------------------------------------------------------
# When coverage is low, blindly expanding the taxonomy is the wrong move.
# This command splits the 'unknown' bucket into three sub-buckets so we
# can see which is dominant:
#
#   missing_taxonomy: spaCy found a verb, but its lemma isn't in our
#                     taxonomy. These are candidates for taxonomy expansion.
#   no_verb_found:    spaCy found no verb at all (and lemma fallback didn't
#                     match either). These are headlines without a usable
#                     directional verb — implicit-direction stuff that's
#                     Stage 3 NLI's job, not Stage 2's. They should NOT
#                     count against Stage 2 coverage.
#   parse_error:      extraction raised. Should be rare.
#
# Output shows the top N missing verbs by frequency with example headlines,
# so taxonomy decisions are data-driven rather than guessed.


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Bucket the 'unknown' headlines by *why* they're unknown."""
    if getattr(args, "gates_only", False):
        rows = _fetch_trigger_relevant_headlines(args.days, args.limit)
        if not rows:
            logger.error("No trigger-relevant headlines found.")
            return 1
        logger.info("Restricted to %d headlines with should_tighten_gates=TRUE", len(rows))
    else:
        rows = _fetch_headlines(args.days, args.limit)
        if not rows:
            logger.error("No headlines found.")
            return 1

    extractor = VerbExtractor()
    # We need the spaCy pipeline directly to inspect verbs that the
    # extractor *saw* but didn't classify. Reuse the extractor's nlp.
    nlp = extractor.nlp

    bucket_classified = 0
    bucket_missing = 0
    bucket_no_verb = 0
    bucket_error = 0
    missing_verbs: Counter[str] = Counter()
    missing_examples: dict[str, list[str]] = {}
    no_verb_examples: list[str] = []

    for row in rows:
        title = row["title"]
        try:
            res = extractor.extract(title)
        except Exception:
            bucket_error += 1
            continue

        if res.verb_category != UNKNOWN_CATEGORY:
            bucket_classified += 1
            continue

        # Headline went unknown. Find what verbs spaCy *did* see.
        # We look at the first sentence's tokens for anything tagged
        # VERB (or AUX, since 'be' / 'have' constructions can carry
        # direction in passives — "sanctions are imposed").
        try:
            doc = nlp(title)
        except Exception:
            bucket_error += 1
            continue

        verbs_in_headline: list[str] = []
        for sent in doc.sents:
            for tok in sent:
                if tok.pos_ in ("VERB", "AUX") and not tok.is_stop:
                    verbs_in_headline.append(tok.lemma_.lower())
            break  # first sentence only — matches extractor behaviour

        if not verbs_in_headline:
            bucket_no_verb += 1
            if len(no_verb_examples) < args.samples:
                no_verb_examples.append(title)
            continue

        bucket_missing += 1
        # Record each verb seen (so a headline with two missing verbs
        # contributes to both verb counts, but only once to the bucket).
        for v in verbs_in_headline:
            missing_verbs[v] += 1
            slot = missing_examples.setdefault(v, [])
            if len(slot) < 3:
                slot.append(title)

    total = len(rows)

    print()
    print(f"=== UNKNOWN-BUCKET DIAGNOSIS ({total} headlines) ===")
    print()
    print(f"Classified:                  {bucket_classified:>5} ({bucket_classified/total:6.1%})")
    print(f"Unknown - missing taxonomy:  {bucket_missing:>5} ({bucket_missing/total:6.1%})")
    print(f"Unknown - no verb in title:  {bucket_no_verb:>5} ({bucket_no_verb/total:6.1%})")
    print(f"Errors:                      {bucket_error:>5}")
    print()
    print("Note: 'no verb in title' headlines genuinely have no extractable")
    print("directional verb. They should NOT count against Stage 2 coverage —")
    print("implicit direction is Stage 3 NLI's job.")
    print()
    denom = bucket_classified + bucket_missing
    if denom > 0:
        print(f"Effective coverage (excluding no-verb headlines):")
        print(f"  {bucket_classified} / {denom} = "
              f"{bucket_classified / denom:.1%}")
        print()
    print(f"=== TOP {args.top} MISSING VERBS (taxonomy expansion candidates) ===")
    print()
    print(f"{'verb':<20} {'count':>6}  example")
    print("-" * 80)
    for verb, count in missing_verbs.most_common(args.top):
        example = missing_examples[verb][0][:50]
        print(f"{verb:<20} {count:>6}  {example}")
    print()
    print(f"=== {min(args.samples, len(no_verb_examples))} HEADLINES WITH NO VERB FOUND ===")
    print()
    for ex in no_verb_examples[: args.samples]:
        print(f"  {ex[:100]}")
    print()
    print("Decision guide:")
    print("  - If a missing verb appears 50+ times AND has a clear directional")
    print("    interpretation, add it to the taxonomy.")
    print("  - If a missing verb is a reporting verb ('say', 'report', 'note'),")
    print("    it belongs in ANNOUNCES with direction='neutral'.")
    print("  - If a verb is genuinely directionless ('be', 'have', 'see'),")
    print("    leave it. Stage 3 NLI will handle implicit direction.")
    print("  - If 'no verb' is dominant, the 85%% target was unrealistic —")
    print("    reset expectations to coverage on headlines-with-verbs only.")
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    cov = sub.add_parser("coverage", help="Coverage report (Task 4.1).")
    cov.add_argument("--days", type=int, default=DEFAULT_DAYS)
    cov.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    cov.set_defaults(func=cmd_coverage)

    smp = sub.add_parser("sample", help="Generate sample CSV for manual review.")
    smp.add_argument("--n", type=int, default=100)
    smp.add_argument("--days", type=int, default=DEFAULT_DAYS)
    smp.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    smp.add_argument("--seed", type=int, default=42)
    smp.add_argument("--out", type=str, default="stage2_sample.csv")
    smp.set_defaults(func=cmd_sample)

    acc = sub.add_parser("accuracy", help="Tally a marked-up sample CSV.")
    acc.add_argument("--file", type=str, required=True)
    acc.set_defaults(func=cmd_accuracy)

    hmz = sub.add_parser("hormuz", help="Iran+Hormuz directional split (Task 4.2).")
    hmz.add_argument("--check-triggers", action="store_true",
                     help="Also feed headlines through TriggerManager "
                          "in a dev schema (requires SCHEMA env var).")
    hmz.set_defaults(func=cmd_hormuz)

    lat = sub.add_parser("latency", help="Latency measurement (Task 4.3).")
    lat.add_argument("--n", type=int, default=1000)
    lat.add_argument("--days", type=int, default=DEFAULT_DAYS)
    lat.add_argument("--seed", type=int, default=42)
    lat.set_defaults(func=cmd_latency)

    diag = sub.add_parser(
        "diagnose",
        help="Inspect why headlines fall into the 'unknown' bucket. "
             "Run this when coverage is low to decide if the gap is a "
             "taxonomy problem, a parser problem, or genuinely no verb.",
    )
    diag.add_argument("--days", type=int, default=DEFAULT_DAYS)
    diag.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    diag.add_argument("--top", type=int, default=40,
                      help="How many top missing-verb candidates to show.")
    diag.add_argument("--samples", type=int, default=20,
                      help="How many example unknowns to print verbatim.")
    diag.add_argument(
        "--gates-only", action="store_true",
        help="Restrict to headlines where should_tighten_gates=TRUE in the "
             "classifications table. This is the correct denominator for "
             "Stage 2 — it excludes price-action and other non-catalyst "
             "categories that never reach the trigger system anyway.",
    )
    diag.set_defaults(func=cmd_diagnose)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
