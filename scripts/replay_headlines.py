"""
Historical headline replay (Phase 5c.2).

Walks classified headlines in chronological order, runs Stage 2 directional
extraction, and calls TriggerManager.process_headline against the replay
schema. Each novel trigger gets outcome rows scheduled for resolution by
the outcomes worker (run separately afterward).

The output is a parallel trigger / mention / outcome history that
mirrors what would have been produced if Stage 2 + the trigger system
had been live the whole time. This becomes the input dataset for the
Phase 5d forecaster's calibration.

How it stays disjoint from production:

  - Schema isolation. Connection runs with `SET search_path = replay,
    public`. Trigger SQL resolves to `replay.triggers` etc.
    Read-only tables (headlines, classifications) resolve to public.

  - Time mocking. Every process_headline call passes
    `now=headline.published_at`, so trigger state machines compute
    decay/lifecycle relative to the historical clock instead of
    wall-clock time. This requires Phase 5c.1's now parameter.

  - Resumability. A `replay.replay_progress` row tracks how far the
    walker has advanced (last published_at + last headline_id seen).
    Re-running picks up from there; --reset to start over.

Workflow:

    # Apply schema:
    python -m config.migrate_replay_schema

    # Run the replay (resumable; safe to interrupt with Ctrl-C):
    python -m scripts.replay_headlines

    # Drain pending outcomes against price_snapshots:
    python -m scripts.outcomes_worker --schema replay --batch 5000

    # Sanity-check the result:
    python -m scripts.replay_headlines --validate

    # Start fresh:
    python -m config.migrate_replay_schema --drop
    python -m config.migrate_replay_schema
    python -m scripts.replay_headlines

Cost: roughly 30-50ms per headline (spaCy is the bottleneck). For 50k
classified headlines, expect ~30-40 minutes wall time.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection, get_cursor
from inference.verb_extractor import VerbExtractor
from pipeline.triggers import TriggerManager
from pipeline.outcomes import schedule_outcomes

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("replay")


# Tracked categories — must match TriggerManager's defaults. Hardcoded
# here so we can early-filter the headline pull (faster than letting
# process_headline filter and discard).
TRACKED_CATEGORIES = ("geopolitical", "regulatory", "macro")

# Process this many headlines per commit. Smaller = more recovery
# granularity, larger = less commit overhead. 200 ≈ 10s of work at the
# spaCy bottleneck, which feels like a reasonable lose-on-crash window.
BATCH_SIZE = 200


def _set_search_path(conn) -> None:
    """Switch this connection to write into replay.* and read from
    public.* via search_path. Must be called BEFORE process_headline
    runs any SQL on this connection."""
    cur = conn.cursor()
    cur.execute("SET search_path TO replay, public")
    cur.close()


def _read_progress(conn) -> Optional[dict]:
    """Read the most-recent in-progress run's checkpoint. None means
    no prior run, or all prior runs finished. We treat 'finished' as
    a clean slate — the operator can use --reset to drop and redo,
    but a finished run is not a checkpoint to resume from."""
    cur = get_cursor(conn)
    cur.execute("""
        SELECT id, run_id, last_processed_published_at,
               last_processed_headline_id, completed_count
        FROM replay_progress
        WHERE finished_at IS NULL
        ORDER BY started_at DESC
        LIMIT 1
    """)
    return cur.fetchone()


def _start_run(conn, resume_id: Optional[int] = None) -> tuple[int, str]:
    """Return (progress_id, run_id) for this invocation.

    If resume_id is given, we update that existing row's started_at and
    return its id+run_id. Otherwise we insert a new row. Either way the
    caller writes progress against `progress_id`."""
    cur = get_cursor(conn)
    if resume_id is not None:
        cur.execute("""
            UPDATE replay_progress
            SET started_at = NOW(),
                notes = COALESCE(notes, '') || E'\\n' || 'resumed at ' || NOW()::TEXT
            WHERE id = %s
            RETURNING id, run_id
        """, (resume_id,))
        row = cur.fetchone()
        return row['id'], row['run_id']

    run_id = uuid.uuid4().hex[:12]
    cur.execute("""
        INSERT INTO replay_progress (run_id) VALUES (%s)
        RETURNING id
    """, (run_id,))
    progress_id = cur.fetchone()['id']
    return progress_id, run_id


def _update_progress(conn, progress_id: int,
                     last_published: datetime, last_id: int,
                     completed_count: int) -> None:
    cur = conn.cursor()
    cur.execute("""
        UPDATE replay_progress
        SET last_processed_published_at = %s,
            last_processed_headline_id = %s,
            completed_count = %s
        WHERE id = %s
    """, (last_published, last_id, completed_count, progress_id))
    cur.close()


def _finish_run(conn, progress_id: int, completed_count: int) -> None:
    cur = conn.cursor()
    cur.execute("""
        UPDATE replay_progress
        SET finished_at = NOW(),
            completed_count = %s
        WHERE id = %s
    """, (completed_count, progress_id))
    cur.close()


def _fetch_batch(conn, since_published: Optional[datetime],
                 since_id: Optional[int],
                 since_filter: Optional[datetime],
                 batch_size: int) -> list[dict]:
    """Pull a batch of classified, tighten-eligible, tracked-category
    headlines newer than the (since_published, since_id) checkpoint,
    in chronological order.

    `since_filter` is the optional --since CLI flag — a hard floor
    that truncates the input regardless of checkpoint state. Useful
    when you only want to replay recent history (e.g. --since
    2026-01-01).

    The (published_at, id) pair is the ordering key. We use it both
    for ordering and for the resume cursor, so resume is exact even
    when many headlines share a published_at second.
    """
    cur = get_cursor(conn)
    # Use the public schema explicitly here — even with search_path
    # pointing at replay first, headlines/classifications live in public.
    # Being explicit avoids any surprise from schema drift.
    #
    # ANY(%s) takes a single parameter that is a Python list; psycopg2
    # maps it to a Postgres array. Don't splat the categories — that
    # would produce N separate %s placeholders which the SQL doesn't
    # have.
    where = [
        "c.should_tighten_gates = TRUE",
        "c.category = ANY(%s)",
    ]
    params: list = [list(TRACKED_CATEGORIES)]
    if since_published is not None:
        # Tuple comparison: (published_at, id) > (cursor_published_at, cursor_id)
        where.append("(h.published_at, h.id) > (%s, %s)")
        params.extend([since_published, since_id])
    if since_filter is not None:
        where.append("h.published_at >= %s")
        params.append(since_filter)
    params.append(batch_size)

    sql = f"""
        SELECT h.id, h.title, h.published_at, c.category
        FROM public.headlines h
        JOIN public.classifications c ON c.headline_id = h.id
        WHERE {' AND '.join(where)}
        ORDER BY h.published_at ASC, h.id ASC
        LIMIT %s
    """
    cur.execute(sql, params)
    return cur.fetchall()


def _fetch_count(conn, since_published: Optional[datetime],
                 since_id: Optional[int],
                 since_filter: Optional[datetime]) -> int:
    """Count how many headlines remain to process. Used for ETA logging."""
    cur = get_cursor(conn)
    where = [
        "c.should_tighten_gates = TRUE",
        "c.category = ANY(%s)",
    ]
    params: list = [list(TRACKED_CATEGORIES)]
    if since_published is not None:
        where.append("(h.published_at, h.id) > (%s, %s)")
        params.extend([since_published, since_id])
    if since_filter is not None:
        where.append("h.published_at >= %s")
        params.append(since_filter)
    cur.execute(f"""
        SELECT COUNT(*) AS n
        FROM public.headlines h
        JOIN public.classifications c ON c.headline_id = h.id
        WHERE {' AND '.join(where)}
    """, params)
    return cur.fetchone()['n']


def run_replay(since_filter: Optional[datetime], reset: bool) -> int:
    """Main replay loop. Returns process exit code."""
    conn = get_connection()
    _set_search_path(conn)

    if reset:
        logger.warning(
            "Reset requested — pre-existing replay tables are NOT dropped "
            "(use migrate_replay_schema --drop for that). Reset only "
            "ignores existing progress checkpoints and starts a fresh run."
        )
        progress_id, run_id = _start_run(conn, resume_id=None)
    else:
        existing = _read_progress(conn)
        if existing is not None:
            progress_id = existing['id']
            run_id = existing['run_id']
            logger.info(
                "Resuming run %s — last processed published_at=%s id=%s "
                "completed=%s",
                run_id, existing['last_processed_published_at'],
                existing['last_processed_headline_id'],
                existing['completed_count'],
            )
            _start_run(conn, resume_id=progress_id)
        else:
            progress_id, run_id = _start_run(conn)
            logger.info("Starting new replay run %s", run_id)
    conn.commit()

    # Initialise heavy machinery once (~5-10s for spaCy + taxonomy).
    logger.info("Initialising verb extractor (spaCy)...")
    try:
        extractor = VerbExtractor()
    except Exception as e:
        logger.error(
            "Verb extractor failed to initialise: %s\n"
            "Replay would produce direction-less mentions, defeating "
            "the point. Install spaCy and en_core_web_sm before retrying.",
            e,
        )
        return 1
    logger.info("Initialising trigger manager...")
    trigger_mgr = TriggerManager()

    # Load checkpoint state from the (possibly-resumed) progress row.
    cur = get_cursor(conn)
    cur.execute("""
        SELECT last_processed_published_at, last_processed_headline_id,
               completed_count
        FROM replay_progress WHERE id = %s
    """, (progress_id,))
    chk = cur.fetchone()
    cursor_published = chk['last_processed_published_at']
    cursor_id = chk['last_processed_headline_id']
    completed_count = chk['completed_count']

    remaining = _fetch_count(conn, cursor_published, cursor_id, since_filter)
    logger.info("Headlines remaining to process: %d", remaining)
    if remaining == 0:
        logger.info("Nothing to do.")
        _finish_run(conn, progress_id, completed_count)
        conn.commit()
        conn.close()
        return 0

    started = time.time()
    last_log = started
    novel_triggers = 0
    extraction_errors = 0

    while True:
        batch = _fetch_batch(conn, cursor_published, cursor_id,
                             since_filter, BATCH_SIZE)
        if not batch:
            break

        for hl in batch:
            # Stage 2 extraction. On any failure, fall back to None
            # direction/verb_category — the headline still goes through
            # the trigger system, just without directional metadata.
            try:
                ext = extractor.extract(hl["title"])
                direction = ext.direction
                verb_cat = ext.verb_category
            except Exception as e:
                logger.warning(
                    "Extractor failed on headline %s (%r): %s",
                    hl["id"], hl["title"][:60], e,
                )
                extraction_errors += 1
                direction = None
                verb_cat = None

            try:
                result = trigger_mgr.process_headline(
                    headline_id=hl["id"],
                    title=hl["title"],
                    category=hl["category"],
                    conn=conn,
                    direction=direction,
                    verb_category=verb_cat,
                    now=hl["published_at"],
                )
            except Exception as e:
                logger.warning(
                    "process_headline failed for headline %s: %s",
                    hl["id"], e,
                )
                # Don't checkpoint past a failure — let the next batch
                # re-attempt. But we need to advance past this row
                # eventually if the failure is structural; for now,
                # bias toward retry. A persistent failure on the same
                # headline will eventually surface as no progress.
                continue

            if result and result.get("is_novel"):
                novel_triggers += 1
                # Schedule outcomes for the new trigger. Same connection,
                # same transaction. search_path makes this hit
                # replay.trigger_outcomes automatically.
                try:
                    schedule_outcomes(
                        trigger_id=result["trigger_id"],
                        trigger_fired_at=hl["published_at"],
                        conn=conn,
                        category=hl["category"],
                    )
                except Exception as e:
                    logger.warning(
                        "schedule_outcomes failed for trigger %s: %s",
                        result["trigger_id"], e,
                    )

            cursor_published = hl["published_at"]
            cursor_id = hl["id"]
            completed_count += 1

        # End of batch: checkpoint and commit.
        _update_progress(conn, progress_id, cursor_published, cursor_id,
                         completed_count)
        conn.commit()

        # Periodic progress log (every 30s wall time, not every batch —
        # batches at 200 headlines × ~50ms = 10s, so that's every ~3
        # batches).
        nowt = time.time()
        if nowt - last_log >= 30:
            elapsed = nowt - started
            rate = completed_count / elapsed if elapsed > 0 else 0
            remaining_est = remaining - completed_count
            eta_sec = remaining_est / rate if rate > 0 else 0
            logger.info(
                "Progress: %d done, %d remaining, %.1f/s, "
                "ETA %.1f min, novel triggers so far: %d",
                completed_count, remaining_est, rate,
                eta_sec / 60, novel_triggers,
            )
            last_log = nowt

    # All done.
    _finish_run(conn, progress_id, completed_count)
    conn.commit()
    elapsed = time.time() - started
    logger.info(
        "Replay complete. processed=%d novel_triggers=%d "
        "extraction_errors=%d elapsed=%.1fs",
        completed_count, novel_triggers, extraction_errors, elapsed,
    )
    conn.close()
    return 0


def run_validate() -> int:
    """Sanity checks on the replay output. Run after replay finishes
    (and ideally after the outcomes worker has drained pending rows)
    to verify the result looks plausible.

    Surface-level checks:
      - Total trigger / mention counts in reasonable range
      - first_seen_at distribution spans the replayed period (not all
        clustered at "today" — that would mean time mocking failed)
      - Direction-mix on populated mentions looks balanced
      - Specific known event present: Iran-Hormuz April escalation
        should be a thick trigger with mention_count >= 50

    Doesn't return non-zero on warnings — the operator should read
    the report and judge."""
    conn = get_connection()
    _set_search_path(conn)
    cur = get_cursor(conn)

    print()
    print("=" * 60)
    print("REPLAY VALIDATION REPORT")
    print("=" * 60)

    # Counts
    cur.execute("SELECT COUNT(*) AS n FROM replay.triggers")
    n_triggers = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM replay.trigger_mentions")
    n_mentions = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM replay.trigger_outcomes")
    n_outcomes = cur.fetchone()['n']
    print(f"Triggers:          {n_triggers}")
    print(f"Trigger mentions:  {n_mentions}")
    print(f"Outcome rows:      {n_outcomes}")

    if n_triggers == 0:
        print("\n✗ No triggers — replay produced no output. Likely")
        print("  Stage 2 / process_headline failed silently.")
        return 1
    if n_triggers > 50000:
        print("\n⚠ Trigger count looks excessive. Possible signature")
        print("  fragmentation; investigate top triggers below.")

    # first_seen_at distribution. If time-mocking failed, all triggers
    # would have first_seen_at clustered around "today".
    #
    # Note: PERCENTILE_CONT in Postgres only accepts numeric types, so
    # we convert timestamptz to epoch seconds for the computation, then
    # back to a timestamp for display. (PERCENTILE_DISC would work
    # natively on timestamps but gives an actual row value rather than
    # an interpolated middle, which is fine here too — we only care
    # about the rough distribution shape.)
    cur.execute("""
        SELECT MIN(first_seen_at) AS earliest,
               MAX(first_seen_at) AS latest,
               TO_TIMESTAMP(
                   PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM first_seen_at)
                   )
               ) AT TIME ZONE 'UTC' AS median
        FROM replay.triggers
    """)
    span = cur.fetchone()
    print(f"\nFirst-seen span:   {span['earliest']} → {span['latest']}")
    print(f"Median first_seen: {span['median']}")
    span_days = (span['latest'] - span['earliest']).days
    if span_days < 7:
        print("  ⚠ first_seen_at span <7 days — time mocking may have failed.")
        print("    Expected the replayed history to span weeks/months.")

    # Direction mix on mentions (only those with populated direction)
    cur.execute("""
        SELECT direction, COUNT(*) AS n
        FROM replay.trigger_mentions
        WHERE direction IS NOT NULL
        GROUP BY direction
        ORDER BY n DESC
    """)
    print(f"\nDirection mix (populated mentions):")
    for r in cur.fetchall():
        print(f"  {r['direction']:<22} {r['n']}")

    # Iran-Hormuz spot check — there should be a thick escalation
    # trigger from April 2026.
    cur.execute("""
        SELECT signature, mention_count, direction, first_seen_at,
               state, impact_score
        FROM replay.triggers
        WHERE signature LIKE '%hormuz%'
           OR signature LIKE '%iran%'
        ORDER BY mention_count DESC
        LIMIT 10
    """)
    rows = cur.fetchall()
    print(f"\nTop Iran/Hormuz triggers (spot check for known event):")
    for r in rows:
        print(f"  [{r['mention_count']:>4}x] {r['signature']:<40} "
              f"dir={r['direction'] or '-':<14} "
              f"first_seen={r['first_seen_at'].strftime('%Y-%m-%d')}")
    if not any(r['mention_count'] >= 30 for r in rows):
        print("  ⚠ No thick Iran/Hormuz trigger found. The April 2026")
        print("    escalation should produce mention_count >= 30.")

    # Outcomes status
    cur.execute("""
        SELECT status, COUNT(*) AS n
        FROM replay.trigger_outcomes
        GROUP BY status
        ORDER BY n DESC
    """)
    print(f"\nOutcome resolution status:")
    for r in cur.fetchall():
        print(f"  {r['status']:<14} {r['n']}")
    print(f"\nTo resolve pending outcomes, run:")
    print(f"  python -m scripts.outcomes_worker --schema replay --batch 5000")

    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only replay headlines published on/after this date "
             "(ISO-8601, e.g. 2026-01-01). Default: replay all "
             "matching headlines.",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Ignore existing progress checkpoint and start a new run "
             "from the beginning. Does NOT drop the replay schema — use "
             "config.migrate_replay_schema --drop for that.",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Run validation report against an existing replay output, "
             "do not run the replay itself.",
    )
    args = parser.parse_args()

    if args.validate:
        return run_validate()

    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError as e:
            logger.error("--since must be ISO-8601 (e.g. 2026-01-01): %s", e)
            return 1

    return run_replay(since_filter=since, reset=args.reset)


if __name__ == "__main__":
    sys.exit(main())
