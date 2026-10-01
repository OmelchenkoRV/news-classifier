"""
Stage 2 drift monitor.

Compares Stage 2 behaviour over a recent window against a historical
baseline to surface drift before it silently breaks production.

Three drift modes monitored:

  1. COVERAGE drop — a smaller % of trigger-relevant headlines are
     getting a directional classification. Most likely cause:
     vocabulary shift in incoming headlines (new event type, new
     RSS source, new news cycle).

  2. CATEGORY / DIRECTION MIX shift — distribution of escalation vs
     de-escalation, or distribution across categories, changes
     materially. Could be real-world signal OR could be a category
     silently breaking (one verb's lemma stops firing).

  3. MISSING-LEMMA TREND — verbs spaCy found in unknowns, ranked by
     recency. Surfaces vocabulary that's appearing now but wasn't
     before — taxonomy expansion candidates.

The tool reports. It does NOT auto-edit the taxonomy. When something
drifts, you edit verb_taxonomy.py manually and ship the change. That's
deliberate — auto-extending the taxonomy from inference outputs creates
feedback loops that amplify errors.

Usage:
    python -m scripts.stage2_monitor                     # default: 14d vs 30d
    python -m scripts.stage2_monitor --recent 7 --baseline 30
    python -m scripts.stage2_monitor --json              # machine-readable
    python -m scripts.stage2_monitor --json > drift.json # for CI/dashboards

Data source: trigger_mentions table (Phase 2). Restricts to mentions
attached to headlines flagged should_tighten_gates=TRUE in their
classification, since those are the only headlines Stage 2 was meant
to classify. Headlines that don't reach the trigger system are out of
scope by design.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection, get_cursor
from inference.verb_extractor import VerbExtractor
from config.verb_taxonomy import UNKNOWN_CATEGORY, all_categories

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# Drift thresholds — when a metric crosses these, we flag it. These are
# defaults chosen for a system seeing ~50-100 trigger-relevant headlines
# per day; tune if your volume is very different.
# ---------------------------------------------------------------------------
COVERAGE_DROP_PP_WARN = 5.0          # warn if coverage drops >5 percentage points
CATEGORY_RELATIVE_DROP_WARN = 0.50   # warn if a category's share drops by >50%
DIRECTION_RATIO_SHIFT_WARN = 2.0     # warn if escalation/de-escalation ratio
                                     # changes by >2x in either direction

# Absolute floor — flag regardless of baseline. Catches the silent-
# degradation case where Stage 2 isn't running at all (extractor failed
# to load, columns aren't being populated). Functioning Stage 2 should
# clear this comfortably even on a slow news day; below it almost
# certainly means production is broken. We also require a minimum recent
# volume so a quiet day with 5 mentions doesn't trip the flag.
COVERAGE_FLOOR_PCT = 0.10            # warn if recent coverage < 10%
COVERAGE_FLOOR_MIN_MENTIONS = 30     # ...with at least this many mentions


# ---------------------------------------------------------------------------
# SQL — pulls per-mention category/direction with timestamps. We use
# trigger_mentions (already has direction, verb_category from Phase 2)
# joined to headlines for published_at. Restricting to should_tighten_gates
# matches the diagnose --gates-only logic from Phase 4.
#
# NB: Postgres does NOT permit parameter substitution inside an INTERVAL
# string literal — `INTERVAL '%s days'` would bind the parameter as a
# quoted string and break the SQL. The correct pattern is to multiply
# an integer parameter by `INTERVAL '1 day'`. Both psycopg and psycopg2
# handle this cleanly.
# ---------------------------------------------------------------------------
WINDOW_SQL = """
    SELECT
        tm.headline_id,
        tm.direction,
        tm.verb_category,
        h.title,
        h.published_at
    FROM trigger_mentions tm
    JOIN headlines h ON h.id = tm.headline_id
    JOIN classifications c ON c.headline_id = h.id
    WHERE c.should_tighten_gates = TRUE
      AND h.published_at >= NOW() - (%s * INTERVAL '1 day')
      AND h.published_at <  NOW() - (%s * INTERVAL '1 day')
"""


def _fetch_window(days_from: int, days_to: int) -> list[dict]:
    """Fetch mentions where published_at is between (now - days_from) and
    (now - days_to). days_from > days_to.

    Example: _fetch_window(14, 0) → last 14 days
             _fetch_window(30, 14) → 14-to-30 days ago
    """
    if days_from <= days_to:
        raise ValueError(f"days_from ({days_from}) must be > days_to ({days_to})")
    conn = get_connection()
    try:
        cur = get_cursor(conn)
        cur.execute(WINDOW_SQL, (days_from, days_to))
        rows = cur.fetchall()
        return [
            {
                "headline_id": r["headline_id"],
                "direction": r["direction"],
                "verb_category": r["verb_category"],
                "title": r["title"],
                "published_at": r["published_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _summarise(rows: list[dict]) -> dict:
    """Compute summary statistics for a window of mentions."""
    total = len(rows)
    if total == 0:
        return {
            "total": 0,
            "coverage": 0.0,
            "classified": 0,
            "unknown": 0,
            "categories": {},
            "directions": {},
        }

    classified = sum(
        1 for r in rows
        if r["verb_category"] and r["verb_category"] != UNKNOWN_CATEGORY
    )
    cat_counts = Counter()
    dir_counts = Counter()
    for r in rows:
        cat = r["verb_category"] or UNKNOWN_CATEGORY
        direction = r["direction"] or "unknown"
        cat_counts[cat] += 1
        dir_counts[direction] += 1

    return {
        "total": total,
        "coverage": classified / total,
        "classified": classified,
        "unknown": total - classified,
        "categories": dict(cat_counts),
        "directions": dict(dir_counts),
    }


def _diff_categories(recent: dict, baseline: dict) -> list[dict]:
    """For each category, compute recent vs baseline shares and absolute /
    relative deltas. Returns sorted by absolute delta in share."""
    cats = set(recent["categories"]) | set(baseline["categories"]) | set(all_categories())
    cats.add(UNKNOWN_CATEGORY)

    rows = []
    for cat in cats:
        r_count = recent["categories"].get(cat, 0)
        b_count = baseline["categories"].get(cat, 0)
        r_share = r_count / recent["total"] if recent["total"] else 0.0
        b_share = b_count / baseline["total"] if baseline["total"] else 0.0
        abs_delta = r_share - b_share
        # Relative change — handle zero baseline cleanly
        if b_share > 0:
            rel_change = (r_share - b_share) / b_share
        else:
            rel_change = float("inf") if r_share > 0 else 0.0
        rows.append({
            "category": cat,
            "recent_count": r_count,
            "baseline_count": b_count,
            "recent_share": r_share,
            "baseline_share": b_share,
            "abs_delta": abs_delta,
            "rel_change": rel_change,
        })
    rows.sort(key=lambda r: abs(r["abs_delta"]), reverse=True)
    return rows


def _diff_directions(recent: dict, baseline: dict) -> dict:
    """Compute escalation/de-escalation ratio shift."""
    def _ratio(d: dict) -> Optional[float]:
        esc = d["directions"].get("escalation", 0)
        de_esc = d["directions"].get("de-escalation", 0)
        if de_esc == 0:
            return None if esc == 0 else float("inf")
        return esc / de_esc

    r_ratio = _ratio(recent)
    b_ratio = _ratio(baseline)

    # Direction-share table for full reporting
    dirs = set(recent["directions"]) | set(baseline["directions"])
    dirs.update(["escalation", "de-escalation", "neutral",
                 "context-dependent", "unknown"])
    rows = []
    for d in dirs:
        r_count = recent["directions"].get(d, 0)
        b_count = baseline["directions"].get(d, 0)
        r_share = r_count / recent["total"] if recent["total"] else 0.0
        b_share = b_count / baseline["total"] if baseline["total"] else 0.0
        rows.append({
            "direction": d,
            "recent_count": r_count,
            "baseline_count": b_count,
            "recent_share": r_share,
            "baseline_share": b_share,
            "abs_delta": r_share - b_share,
        })
    rows.sort(key=lambda r: abs(r["abs_delta"]), reverse=True)

    return {
        "rows": rows,
        "recent_esc_de_ratio": r_ratio,
        "baseline_esc_de_ratio": b_ratio,
    }


def _missing_lemmas(rows: list[dict], extractor: VerbExtractor, top: int = 15) -> list[dict]:
    """Re-run extraction on unknown headlines to recover what verbs spaCy
    saw but weren't classified. Same logic as diagnose's missing-verbs
    counter but scoped to the recent window only.

    Returns top-N with frequency and an example title.
    """
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    nlp = extractor.nlp

    unknown_rows = [
        r for r in rows
        if not r["verb_category"] or r["verb_category"] == UNKNOWN_CATEGORY
    ]

    for r in unknown_rows:
        title = r["title"]
        try:
            doc = nlp(title)
        except Exception:
            continue
        for sent in doc.sents:
            for tok in sent:
                if tok.pos_ in ("VERB", "AUX") and not tok.is_stop:
                    lemma = tok.lemma_.lower()
                    counts[lemma] += 1
                    if lemma not in examples:
                        examples[lemma] = title
            break  # first sentence only

    return [
        {"lemma": lemma, "count": n, "example": examples[lemma][:80]}
        for lemma, n in counts.most_common(top)
    ]


# ---------------------------------------------------------------------------
# Flag generation — inspect the deltas, decide what's worth a warning
# ---------------------------------------------------------------------------
def _generate_flags(
    recent: dict, baseline: dict,
    cat_diffs: list[dict], dir_diffs: dict,
) -> list[dict]:
    flags = []

    # Coverage floor — fires regardless of baseline. Catches silent
    # degradation where Stage 2 isn't running at all (extractor failed
    # to load, columns aren't populated). This is the failure mode that
    # bit us in the May 2026 outage — a week of NULL directions because
    # spaCy wasn't installed in the container, and the delta-based
    # coverage_drop flag couldn't see it because both windows were
    # similarly empty.
    if (recent["total"] >= COVERAGE_FLOOR_MIN_MENTIONS
            and recent["coverage"] < COVERAGE_FLOOR_PCT):
        flags.append({
            "level": "warn",
            "kind": "coverage_implausibly_low",
            "message": (
                f"Coverage is {recent['coverage']:.1%} on {recent['total']} "
                f"recent mentions — below the {COVERAGE_FLOOR_PCT:.0%} floor. "
                f"This is well below what a working Stage 2 produces and "
                f"strongly suggests the extractor is not running or not "
                f"persisting its output. Check live.py startup logs for "
                f"'Stage 2 VerbExtractor unavailable'. Run "
                f"`python -m scripts.stage2_monitor --debug` to diagnose."
            ),
        })

    # Coverage drop (delta-based)
    coverage_delta_pp = (recent["coverage"] - baseline["coverage"]) * 100
    if recent["total"] >= 50 and coverage_delta_pp < -COVERAGE_DROP_PP_WARN:
        flags.append({
            "level": "warn",
            "kind": "coverage_drop",
            "message": (
                f"Coverage dropped {-coverage_delta_pp:.1f}pp "
                f"({baseline['coverage']:.1%} → {recent['coverage']:.1%}). "
                f"Likely vocabulary shift; check missing-lemma report."
            ),
        })

    # Per-category significant share drops (rel change <-50%, with min
    # baseline support of 10 mentions to avoid noise on rare categories)
    for cd in cat_diffs:
        if (cd["category"] != UNKNOWN_CATEGORY
                and cd["baseline_count"] >= 10
                and cd["rel_change"] != float("inf")
                and cd["rel_change"] < -CATEGORY_RELATIVE_DROP_WARN):
            flags.append({
                "level": "warn",
                "kind": "category_drop",
                "message": (
                    f"Category {cd['category']} share dropped "
                    f"{-cd['rel_change']:.0%} relative "
                    f"({cd['baseline_share']:.1%} → {cd['recent_share']:.1%}). "
                    f"Possibly a lemma silently broke — check production logs "
                    f"or run hormuz validation."
                ),
            })

    # Escalation/de-escalation ratio shift
    rr = dir_diffs["recent_esc_de_ratio"]
    br = dir_diffs["baseline_esc_de_ratio"]
    if rr is not None and br is not None and rr != float("inf") and br != float("inf") and br > 0:
        ratio_shift = rr / br if br > 0 else float("inf")
        if ratio_shift > DIRECTION_RATIO_SHIFT_WARN or ratio_shift < (1 / DIRECTION_RATIO_SHIFT_WARN):
            flags.append({
                "level": "info",
                "kind": "direction_ratio_shift",
                "message": (
                    f"Escalation/de-escalation ratio shifted "
                    f"{br:.2f} → {rr:.2f} ({ratio_shift:.1f}x). "
                    f"May reflect real news-cycle change; verify against "
                    f"world events before treating as drift."
                ),
            })

    # Sanity: too few rows in either window means we can't conclude much
    if recent["total"] < 20:
        flags.append({
            "level": "info",
            "kind": "low_volume_recent",
            "message": (
                f"Recent window has only {recent['total']} mentions. "
                f"Statistics are noisy; consider widening --recent."
            ),
        })
    if baseline["total"] < 100:
        flags.append({
            "level": "info",
            "kind": "low_volume_baseline",
            "message": (
                f"Baseline window has only {baseline['total']} mentions. "
                f"Drift comparisons are noisy; consider widening --baseline."
            ),
        })

    return flags


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _print_report(report: dict) -> None:
    """Plain-text drift report."""
    rec = report["recent"]
    base = report["baseline"]
    print()
    print("=" * 70)
    print(f"STAGE 2 DRIFT MONITOR")
    print(f"  Recent:   last {report['recent_days']} days "
          f"({rec['total']} mentions)")
    print(f"  Baseline: {report['baseline_days_from']} to "
          f"{report['baseline_days_to']} days ago "
          f"({base['total']} mentions)")
    print(f"  Generated at: {report['generated_at']}")
    print("=" * 70)
    print()

    # Flags
    if report["flags"]:
        print("FLAGS:")
        for f in report["flags"]:
            symbol = "⚠ " if f["level"] == "warn" else "ℹ "
            print(f"  {symbol}[{f['kind']}] {f['message']}")
        print()
    else:
        print("✓ No drift flags raised.\n")

    # Coverage summary
    print("COVERAGE")
    print(f"  Recent:    {rec['classified']}/{rec['total']} = "
          f"{rec['coverage']:.1%}")
    print(f"  Baseline:  {base['classified']}/{base['total']} = "
          f"{base['coverage']:.1%}")
    delta_pp = (rec["coverage"] - base["coverage"]) * 100
    sign = "+" if delta_pp >= 0 else ""
    print(f"  Delta:     {sign}{delta_pp:.1f}pp")
    print()

    # Direction mix
    print("DIRECTION MIX")
    print(f"  {'direction':<22} {'recent':>10} {'baseline':>10} {'delta':>10}")
    for r in report["direction_diffs"]["rows"]:
        sign = "+" if r["abs_delta"] >= 0 else ""
        print(f"  {r['direction']:<22} {r['recent_share']:>9.1%} "
              f"{r['baseline_share']:>9.1%} {sign}{r['abs_delta']*100:>8.1f}pp")
    rr = report["direction_diffs"]["recent_esc_de_ratio"]
    br = report["direction_diffs"]["baseline_esc_de_ratio"]
    rr_str = f"{rr:.2f}" if rr is not None and rr != float("inf") else "n/a"
    br_str = f"{br:.2f}" if br is not None and br != float("inf") else "n/a"
    print(f"  Escalation/de-escalation ratio: {br_str} (baseline) → {rr_str} (recent)")
    print()

    # Category breakdown — show top movers only to keep output readable
    print("CATEGORY MIX (top 8 by absolute delta)")
    print(f"  {'category':<22} {'recent':>10} {'baseline':>10} {'delta':>10}")
    for cd in report["category_diffs"][:8]:
        sign = "+" if cd["abs_delta"] >= 0 else ""
        print(f"  {cd['category']:<22} {cd['recent_share']:>9.1%} "
              f"{cd['baseline_share']:>9.1%} {sign}{cd['abs_delta']*100:>8.1f}pp")
    print()

    # Missing lemmas
    if report["missing_lemmas"]:
        print(f"TOP MISSING LEMMAS IN RECENT WINDOW (taxonomy candidates)")
        print(f"  {'lemma':<18} {'count':>6}  example")
        print("  " + "-" * 60)
        for ml in report["missing_lemmas"]:
            print(f"  {ml['lemma']:<18} {ml['count']:>6}  {ml['example']}")
        print()

    print("To act on this report: edit config/verb_taxonomy.py, run pytest, ship.")
    print()


def _print_json(report: dict) -> None:
    """JSON output for dashboards / CI / piping."""
    # datetime not JSON-serialisable; coerce to ISO-string
    def _coerce(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if o == float("inf"):
            return "inf"
        if o == float("-inf"):
            return "-inf"
        raise TypeError(f"can't serialise {type(o)}")
    print(json.dumps(report, indent=2, default=_coerce))


# ---------------------------------------------------------------------------
# Debug mode — diagnose empty-data situations
# ---------------------------------------------------------------------------
def _run_debug(recent: int, baseline: int) -> int:
    """Diagnostic mode: surface why the monitor would return empty data.

    Specifically checks:
      - Does trigger_mentions have direction/verb_category columns?
        (i.e. did the migration apply?)
      - How many trigger_mentions rows have NULL direction / verb_category?
      - How many mentions exist in the recent and baseline windows, with
        and without the should_tighten_gates=TRUE filter?
      - Earliest / latest mention dates, to bound the data range.
    """
    conn = get_connection()
    try:
        cur = get_cursor(conn)

        print("\n=== STAGE 2 MONITOR — DEBUG MODE ===\n")

        # 1. Schema check — did the migration apply?
        print("1. Schema state for trigger_mentions:")
        cur.execute("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'trigger_mentions'
              AND column_name IN ('direction', 'verb_category')
            ORDER BY column_name
        """)
        rows = cur.fetchall()
        if not rows:
            print("   ✗ direction/verb_category columns NOT FOUND")
            print("     → run: python -m config.migrate_directional")
            return 1
        for r in rows:
            print(f"   ✓ {r['column_name']:<20} ({r['data_type']})")

        # 2. NULL ratio for the new columns — is Stage 2 actually populating them?
        print("\n2. NULL counts on trigger_mentions (last 14 days of mentions):")
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE direction IS NULL) AS null_direction,
                COUNT(*) FILTER (WHERE verb_category IS NULL) AS null_verb_category,
                COUNT(*) FILTER (WHERE direction IS NOT NULL) AS has_direction,
                COUNT(*) FILTER (WHERE verb_category IS NOT NULL) AS has_verb_category
            FROM trigger_mentions tm
            JOIN headlines h ON h.id = tm.headline_id
            WHERE h.published_at > NOW() - (14 * INTERVAL '1 day')
        """)
        r = cur.fetchone()
        total = r['total']
        if total == 0:
            print("   ✗ ZERO mentions in last 14 days. Either the pipeline isn't")
            print("     running, or no headlines reached the trigger system.")
        else:
            null_dir = r['null_direction']
            null_vc = r['null_verb_category']
            has_dir = r['has_direction']
            has_vc = r['has_verb_category']
            print(f"   total mentions:              {total}")
            print(f"   direction is NULL:           {null_dir} ({null_dir/total:.1%})")
            print(f"   verb_category is NULL:       {null_vc} ({null_vc/total:.1%})")
            print(f"   direction populated:         {has_dir} ({has_dir/total:.1%})")
            print(f"   verb_category populated:     {has_vc} ({has_vc/total:.1%})")
            if null_dir == total:
                print("\n   ✗ ALL mentions have NULL direction. Stage 2 is not")
                print("     populating these columns. Most likely cause: live.py")
                print("     was running before Phase 3 was deployed, OR spaCy /")
                print("     en_core_web_sm isn't installed in production.")
                print("     Check live.py startup logs for:")
                print("       'Stage 2 VerbExtractor unavailable'")

        # 3. Volume in the recent and baseline windows (the actual queries
        #    the monitor runs).
        print(f"\n3. Mention volume by window (with should_tighten_gates=TRUE filter):")
        for label, days_from, days_to in [
            ("recent", recent, 0),
            ("baseline", recent + baseline, recent),
        ]:
            cur.execute("""
                SELECT COUNT(*) AS n
                FROM trigger_mentions tm
                JOIN headlines h ON h.id = tm.headline_id
                JOIN classifications c ON c.headline_id = h.id
                WHERE c.should_tighten_gates = TRUE
                  AND h.published_at >= NOW() - (%s * INTERVAL '1 day')
                  AND h.published_at <  NOW() - (%s * INTERVAL '1 day')
            """, (days_from, days_to))
            n = cur.fetchone()['n']
            print(f"   {label:<10} ({days_from}d to {days_to}d ago): {n} mentions")

        # 4. Same windows WITHOUT the should_tighten_gates filter — to see
        #    whether the filter is the cause of empty baselines.
        print(f"\n4. Same windows WITHOUT should_tighten_gates filter:")
        for label, days_from, days_to in [
            ("recent", recent, 0),
            ("baseline", recent + baseline, recent),
        ]:
            cur.execute("""
                SELECT COUNT(*) AS n
                FROM trigger_mentions tm
                JOIN headlines h ON h.id = tm.headline_id
                WHERE h.published_at >= NOW() - (%s * INTERVAL '1 day')
                  AND h.published_at <  NOW() - (%s * INTERVAL '1 day')
            """, (days_from, days_to))
            n = cur.fetchone()['n']
            print(f"   {label:<10} ({days_from}d to {days_to}d ago): {n} mentions")
        print("   (If these numbers are healthy but #3 is empty, classifications")
        print("    table doesn't have rows for those headlines — check whether")
        print("    the classifier was running back then.)")

        # 5. Earliest and latest published_at in trigger_mentions, to bound
        #    the question of "how far back does this data go?"
        print(f"\n5. Date range in trigger_mentions (via headlines):")
        cur.execute("""
            SELECT
                MIN(h.published_at) AS earliest,
                MAX(h.published_at) AS latest,
                COUNT(*) AS total
            FROM trigger_mentions tm
            JOIN headlines h ON h.id = tm.headline_id
        """)
        r = cur.fetchone()
        print(f"   earliest mention: {r['earliest']}")
        print(f"   latest mention:   {r['latest']}")
        print(f"   total mentions:   {r['total']}")

        print("\n=== END DEBUG ===\n")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--recent", type=int, default=14,
        help="Recent window in days (default: 14).",
    )
    p.add_argument(
        "--baseline", type=int, default=30,
        help="Baseline window in days, ending where the recent window starts. "
             "If --recent=14 and --baseline=30, baseline = days 15–44 ago "
             "(default: 30).",
    )
    p.add_argument(
        "--top-missing", type=int, default=15,
        help="How many missing-lemma candidates to surface (default: 15).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of plain text. For dashboards or CI gates.",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Print diagnostic queries to identify why the report is empty. "
             "Use this when the monitor reports 0 mentions or 0%% coverage "
             "and you suspect missing data rather than real drift.",
    )
    args = p.parse_args()

    if args.recent <= 0 or args.baseline <= 0:
        logger.error("--recent and --baseline must be positive")
        return 1

    if args.debug:
        return _run_debug(args.recent, args.baseline)

    # Recent window: last N days. Baseline: the M days ending where
    # recent starts (no overlap).
    recent_rows = _fetch_window(days_from=args.recent, days_to=0)
    baseline_rows = _fetch_window(
        days_from=args.recent + args.baseline,
        days_to=args.recent,
    )

    if not recent_rows and not baseline_rows:
        logger.error(
            "No data in either window. Check that classifications and "
            "trigger_mentions tables have rows in the requested ranges."
        )
        return 1

    recent_summary = _summarise(recent_rows)
    baseline_summary = _summarise(baseline_rows)
    cat_diffs = _diff_categories(recent_summary, baseline_summary)
    dir_diffs = _diff_directions(recent_summary, baseline_summary)
    flags = _generate_flags(recent_summary, baseline_summary, cat_diffs, dir_diffs)

    # Missing-lemma analysis on recent window only — that's where we'd
    # take action, and running spaCy over the baseline too doubles the
    # cost without adding actionable signal.
    missing = []
    if recent_rows:
        try:
            extractor = VerbExtractor()
            missing = _missing_lemmas(recent_rows, extractor, top=args.top_missing)
        except Exception as e:
            logger.warning(f"Missing-lemma analysis skipped: {e}")

    report = {
        "generated_at": datetime.now(timezone.utc),
        "recent_days": args.recent,
        "baseline_days_from": args.recent + args.baseline,
        "baseline_days_to": args.recent,
        "recent": recent_summary,
        "baseline": baseline_summary,
        "category_diffs": cat_diffs,
        "direction_diffs": dir_diffs,
        "missing_lemmas": missing,
        "flags": flags,
    }

    if args.json:
        _print_json(report)
    else:
        _print_report(report)

    # Exit code: nonzero if any warn-level flag fired (useful for CI)
    has_warn = any(f["level"] == "warn" for f in flags)
    return 1 if has_warn else 0


if __name__ == "__main__":
    sys.exit(main())
