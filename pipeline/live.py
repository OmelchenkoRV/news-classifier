"""
Live Pipeline Service — Test Mode
====================================

Runs continuously:
  1. Every 5 minutes: polls all active RSS feeds
  2. Stores new headlines in headlines table (with deduplication)
  3. Classifies each new headline with the trained DistilBERT model
  4. Writes predictions to classifications table
  5. Logs alerts for high-confidence causal headlines

Designed to run in Docker for a week of continuous operation.

Configuration via environment variables:
  POLL_INTERVAL_SECONDS=300  (default 5 min)
  LOG_LEVEL=INFO
  ALERT_ON_TIGHTEN=true      (log loud alerts when gate tightening triggers)

Usage:
    python -m pipeline.live
"""

import os
import sys
import time
import logging
import signal
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import get_connection, get_cursor
from collectors.rss_collector import collect_once as rss_collect
from inference.classifier import HeadlineClassifierInference
from pipeline.outcomes import schedule_outcomes
from pipeline.alerter import insert_alert
from pipeline.forecaster import priors_for_trigger

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
ALERT_ON_TIGHTEN = os.getenv("ALERT_ON_TIGHTEN", "true").lower() == "true"
MODEL_VERSION = os.getenv("MODEL_VERSION", "v2")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("pipeline")


class LivePipeline:
    def __init__(self):
        logger.info("Initializing live pipeline...")
        self.classifier = HeadlineClassifierInference()
        from pipeline.triggers import TriggerManager
        self.trigger_manager = TriggerManager()

        # Stage 2 verb extractor. If spaCy or its model isn't available,
        # we degrade gracefully: the pipeline keeps running with pre-Stage-2
        # behaviour (no directional info on triggers/alerts). This matches
        # the existing pattern in this file — components fail loudly via
        # logs but don't take the loop down.
        self.verb_extractor = self._init_verb_extractor()

        self.shutdown_requested = False
        self.stats = {
            "cycles": 0,
            "headlines_collected": 0,
            "headlines_classified": 0,
            "causal_alerts": 0,
            "novel_events": 0,
            "gate_tighten_alerts": 0,
            "suppressed_alerts": 0,
            "direction_changes": 0,
            "errors": 0,
            "started_at": datetime.now(timezone.utc),
        }
        logger.info(f"Pipeline ready. Poll interval: {POLL_INTERVAL}s")

    def _init_verb_extractor(self):
        """Lazy-load the spaCy-backed VerbExtractor. Returns None on failure
        and logs a prominent warning — the pipeline runs without Stage 2
        rather than refusing to start."""
        try:
            from inference.verb_extractor import VerbExtractor
            extractor = VerbExtractor()
            logger.info("Stage 2 VerbExtractor loaded (en_core_web_sm)")
            return extractor
        except Exception as e:
            logger.warning(
                "Stage 2 VerbExtractor unavailable (%s). "
                "Pipeline will run WITHOUT directional classification. "
                "Install with: pip install spacy && "
                "python -m spacy download en_core_web_sm",
                e,
            )
            return None

    def classify_unclassified_headlines(self):
        """Find headlines without classifications and classify them."""
        conn = get_connection()
        cur = get_cursor(conn)

        # Find headlines that haven't been classified by this model version
        cur.execute("""
            SELECT h.id, h.title, h.source_name, h.published_at
            FROM headlines h
            LEFT JOIN classifications c 
                ON c.headline_id = h.id AND c.model_version = %s
            WHERE c.id IS NULL
              AND h.collected_at > NOW() - INTERVAL '24 hours'
            ORDER BY h.published_at DESC
            LIMIT 500
        """, (MODEL_VERSION,))

        unclassified = cur.fetchall()
        if not unclassified:
            conn.close()
            # Five-tuple to match the success-path return below:
            # (classified, causal, tighten, novel, suppressed). Was
            # previously 3-tuple, which caused "not enough values to
            # unpack (expected 5, got 3)" on quiet cycles where no
            # headlines needed classification.
            return 0, 0, 0, 0, 0

        classified_count = 0
        causal_count = 0
        tighten_count = 0
        novel_count = 0
        suppressed_count = 0

        for row in unclassified:
            start_time = time.time()
            try:
                result = self.classifier.classify(row["title"])
                processing_ms = int((time.time() - start_time) * 1000)

                # ── Event novelty check ─────────────────────────────
                # The classifier says "geopolitical/macro/regulatory" is
                # potentially market-moving. The trigger manager decides
                # if THIS headline is actually novel or just more coverage
                # of an existing event.
                trigger_result = None
                is_novel = False
                trigger_id = None
                # Stage 2 outputs (None if extractor unavailable or the
                # headline doesn't reach the trigger branch). Used for
                # the alert log and recorded on the trigger mention.
                verb_direction = None
                verb_category = None

                if result["should_tighten_gates"]:
                    # ── Stage 2: directional verb extraction ──────
                    # Run spaCy only on the headlines that actually
                    # reach the trigger system. ~30ms/headline; bounding
                    # this to should_tighten_gates avoids paying that
                    # cost for the bulk of headlines that won't be
                    # tracked anyway. Failures here are non-fatal —
                    # we fall back to the pre-Stage-2 behaviour of
                    # passing direction=None to the trigger manager.
                    if self.verb_extractor is not None:
                        try:
                            extraction = self.verb_extractor.extract(row["title"])
                            verb_direction = extraction.direction
                            verb_category = extraction.verb_category
                        except Exception as e:
                            logger.warning(
                                f"Stage 2 extraction failed for headline "
                                f"{row['id']}: {e}"
                            )

                    try:
                        trigger_result = self.trigger_manager.process_headline(
                            row["id"], row["title"], result["category"],
                            conn=conn,
                            direction=verb_direction,
                            verb_category=verb_category,
                        )
                        if trigger_result:
                            trigger_id = trigger_result["trigger_id"]
                            is_novel = trigger_result["is_novel"]

                            # Phase 5a: when a brand-new trigger is created,
                            # schedule outcome lookups at the configured
                            # horizons. Existing triggers don't need
                            # rescheduling — they already have outcome rows
                            # from when they were created. Use the
                            # headline's published_at as the fire time
                            # rather than NOW(); for live traffic these
                            # are within seconds, but the same code path
                            # handles backfilled / replayed triggers
                            # correctly when their published_at is
                            # historical.
                            if is_novel:
                                try:
                                    sched = schedule_outcomes(
                                        trigger_id=trigger_id,
                                        trigger_fired_at=row["published_at"],
                                        conn=conn,
                                    )
                                    logger.debug(
                                        "Scheduled %d outcome rows for "
                                        "trigger %s (skipped %d existing)",
                                        sched.rows_created, trigger_id,
                                        sched.rows_skipped,
                                    )
                                except Exception as e:
                                    # Non-fatal: outcome scheduling failure
                                    # shouldn't block the alert path. The
                                    # trigger still exists; we just lose
                                    # the prior data for it. Log loudly so
                                    # this gets noticed.
                                    logger.warning(
                                        "Failed to schedule outcomes for "
                                        "trigger %s: %s", trigger_id, e,
                                    )
                    except Exception as e:
                        logger.warning(f"Trigger processing error: {e}")

                # Tighten gate criteria — strict to control alert volume:
                #
                #   1. NOVEL events: yes, always tighten (this is THE catalyst)
                #   2. ACTIVE trigger with high impact score (>= 0.85, meaning
                #      first 2 hours of trigger lifetime): yes, this is fresh
                #      breaking news worth alerting on
                #   3. Anything else: suppress
                #
                # The 0.85 threshold corresponds to ~2 hours of trigger life
                # in our decay curve (1.0 at 0h, 0.7 at 24h). After that, the
                # narrative is becoming priced-in and additional headlines
                # are coverage rather than catalysts.
                effective_tighten = False
                if not result["should_tighten_gates"]:
                    # classifier already says no
                    pass
                elif trigger_result is None:
                    # category not tracked or headline too generic — fall
                    # back to classifier's decision (rare path)
                    effective_tighten = True
                elif is_novel:
                    # genuinely novel event — alert
                    effective_tighten = True
                elif trigger_result.get('impact_score', 0) >= 0.85:
                    # very fresh ACTIVE trigger (first ~2h) — alert
                    effective_tighten = True
                else:
                    # follow-up to existing narrative — suppress
                    effective_tighten = False
                    suppressed_count += 1

                cur.execute("""
                    INSERT INTO classifications
                        (headline_id, impact_level, impact_confidence,
                         category, category_confidence, is_causal,
                         news_impact_score, should_tighten_gates,
                         model_version, processing_time_ms,
                         trigger_id, is_novel_event)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (headline_id, model_version) DO NOTHING
                """, (
                    row["id"],
                    result["impact_level"],
                    result["impact_confidence"],
                    result["category"],
                    result["category_confidence"],
                    result["is_causal"],
                    result["news_impact_score"],
                    effective_tighten,
                    MODEL_VERSION,
                    processing_ms,
                    trigger_id,
                    is_novel,
                ))

                classified_count += 1
                if result["is_causal"]:
                    causal_count += 1
                if is_novel:
                    novel_count += 1

                # Alert only on NOVEL gate-tightening events
                if effective_tighten:
                    tighten_count += 1
                    is_dir_change = bool(
                        trigger_result
                        and trigger_result.get("is_direction_change")
                    )
                    if is_dir_change:
                        self.stats["direction_changes"] += 1
                    if ALERT_ON_TIGHTEN:
                        age_min = (datetime.now(timezone.utc) - row["published_at"]).total_seconds() / 60
                        novel_tag = "NOVEL" if is_novel else "ACTIVE"
                        sig = trigger_result["signature"][:30] if trigger_result else "-"
                        # Direction tag: only render strong directions
                        # ('escalation' / 'de-escalation') — neutral /
                        # context-dependent / unknown / None aren't
                        # informative enough to clutter the alert.
                        trig_dir = (trigger_result or {}).get("direction")
                        dir_tag = ""
                        if trig_dir in ("escalation", "de-escalation"):
                            dir_tag = f" dir={trig_dir}"
                        if is_dir_change:
                            dir_tag += " FLIP"
                        logger.warning(
                            f"GATE TIGHTEN [{novel_tag}] [{result['category']}]"
                            f"{dir_tag} "
                            f"trigger={sig} "
                            f"age={age_min:.0f}min "
                            f"source={row['source_name']} | "
                            f"{row['title'][:70]}"
                        )

                    # Phase 5b: persist the alert. Insert is denormalised
                    # — we copy headline title, trigger signature, and
                    # current trigger state snapshot onto the row so the
                    # consumer (crypto-yield-management-system) doesn't
                    # have to join. A Postgres trigger broadcasts the
                    # new id on the `alerts_new` NOTIFY channel; we
                    # don't pg_notify from here.
                    #
                    # Failure here is non-fatal: log loudly but don't
                    # let an alert-table problem (FK, unique violation,
                    # disk full) crash the live pipeline. The
                    # classification has already been persisted; the
                    # GATE TIGHTEN log line above is the durable
                    # record either way.
                    # Phase 5d: compute priors as-of-now from historical
                    # outcomes. Read-only, scoped to the same connection
                    # so it shares the per-headline transaction. If the
                    # trigger has no historical kin (or the forecaster
                    # query errors), fall back to None — alerts work
                    # without priors, the consumer just doesn't see
                    # them on this row.
                    priors_dict = None
                    sig_for_priors = (
                        trigger_result.get("signature")
                        if trigger_result else None
                    )
                    if sig_for_priors:
                        try:
                            priors_dict = priors_for_trigger(
                                conn=conn,
                                signature=sig_for_priors,
                                direction=verb_direction,
                                # As-of-fire-time honesty: only use
                                # outcomes whose trigger fired strictly
                                # before this alert. With wall-clock
                                # passed as `before`, we get all
                                # historically-completed outcomes
                                # (which is what we want — they all
                                # predate this moment).
                                before=datetime.now(timezone.utc),
                            )
                            if not priors_dict:
                                # Empty dict → no cells with n>=MIN_N.
                                # Store as None so consumer can quickly
                                # check "do I have priors?" without
                                # introspecting a dict.
                                priors_dict = None
                        except Exception as e:
                            logger.warning(
                                "Forecaster failed for trigger %s: %s",
                                sig_for_priors, e,
                            )
                            priors_dict = None

                    try:
                        alert_result = insert_alert(
                            conn,
                            headline_id=row["id"],
                            headline_title=row["title"],
                            headline_published_at=row["published_at"],
                            category=result["category"],
                            is_novel=is_novel,
                            is_direction_change=is_dir_change,
                            trigger_id=trigger_id,
                            trigger_signature=(
                                trigger_result.get("signature")
                                if trigger_result else None
                            ),
                            trigger_display_name=(
                                trigger_result.get("display_name")
                                if trigger_result else None
                            ),
                            direction=verb_direction,
                            verb_category=verb_category,
                            impact_score=(
                                trigger_result.get("impact_score")
                                if trigger_result else None
                            ),
                            mention_count=(
                                trigger_result.get("mention_count")
                                if trigger_result else None
                            ),
                            priors=priors_dict,
                        )
                        if alert_result.was_duplicate:
                            logger.debug(
                                "Alert insert suppressed (duplicate) for "
                                "headline %s", row["id"],
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to insert alert for headline %s: %s",
                            row["id"], e,
                        )

            except Exception as e:
                logger.error(f"Classification error for headline {row['id']}: {e}")
                self.stats["errors"] += 1
                conn.rollback()

            if classified_count % 50 == 0:
                conn.commit()

        conn.commit()
        conn.close()
        return classified_count, causal_count, tighten_count, novel_count, suppressed_count

    def run_cycle(self):
        """One complete collection + classification cycle."""
        cycle_start = time.time()
        logger.info("=" * 60)
        logger.info(f"Cycle {self.stats['cycles'] + 1} starting")

        # Step 1: Collect new headlines from RSS
        try:
            before_count = self._count_headlines()
            rss_collect()
            after_count = self._count_headlines()
            new_headlines = after_count - before_count
            self.stats["headlines_collected"] += new_headlines
            logger.info(f"  Collected {new_headlines} new headlines")
        except Exception as e:
            logger.error(f"RSS collection failed: {e}")
            self.stats["errors"] += 1
            new_headlines = 0

        # Step 2: Classify unclassified headlines
        try:
            classified, causal, tighten, novel, suppressed = self.classify_unclassified_headlines()
            self.stats["headlines_classified"] += classified
            self.stats["causal_alerts"] += causal
            self.stats["gate_tighten_alerts"] += tighten
            self.stats["novel_events"] += novel
            self.stats["suppressed_alerts"] += suppressed
            logger.info(
                f"  Classified {classified} headlines "
                f"({causal} causal, {novel} novel events, "
                f"{tighten} gate-tightening, {suppressed} suppressed as follow-ups)"
            )
        except Exception as e:
            logger.error(f"Classification failed: {e}")
            self.stats["errors"] += 1

        # Step 3: Run trigger decay every 12 cycles (~1 hour at 5min interval)
        if self.stats["cycles"] % 12 == 0:
            try:
                decay_stats = self.trigger_manager.decay_triggers()
                logger.info(f"  Trigger decay: {decay_stats}")
            except Exception as e:
                logger.error(f"Trigger decay failed: {e}")

        # Report cycle stats
        cycle_time = time.time() - cycle_start
        self.stats["cycles"] += 1
        uptime = datetime.now(timezone.utc) - self.stats["started_at"]

        logger.info(
            f"Cycle done in {cycle_time:.1f}s | "
            f"Uptime: {uptime} | "
            f"Total: {self.stats['headlines_classified']} classified, "
            f"{self.stats['novel_events']} novel, "
            f"{self.stats['gate_tighten_alerts']} alerts"
        )

    def _count_headlines(self) -> int:
        conn = get_connection()
        cur = get_cursor(conn)
        cur.execute("SELECT COUNT(*) as cnt FROM headlines")
        count = cur.fetchone()["cnt"]
        conn.close()
        return count

    def run_forever(self):
        """Main loop. Runs until shutdown signal."""
        logger.info("Starting live pipeline loop...")
        logger.info(f"  Poll interval: {POLL_INTERVAL}s ({POLL_INTERVAL/60:.1f}min)")
        logger.info(f"  Model version: {MODEL_VERSION}")

        while not self.shutdown_requested:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
                break
            except Exception as e:
                logger.exception(f"Unexpected error in cycle: {e}")
                self.stats["errors"] += 1

            if self.shutdown_requested:
                break

            # Sleep until next cycle
            sleep_end = time.time() + POLL_INTERVAL
            while time.time() < sleep_end and not self.shutdown_requested:
                time.sleep(min(5, sleep_end - time.time()))

        self._log_final_stats()

    def _log_final_stats(self):
        uptime = datetime.now(timezone.utc) - self.stats["started_at"]
        logger.info("=" * 60)
        logger.info("Pipeline shutting down. Final stats:")
        logger.info(f"  Uptime:              {uptime}")
        logger.info(f"  Cycles completed:    {self.stats['cycles']}")
        logger.info(f"  Headlines collected: {self.stats['headlines_collected']}")
        logger.info(f"  Headlines classified: {self.stats['headlines_classified']}")
        logger.info(f"  Causal alerts:       {self.stats['causal_alerts']}")
        logger.info(f"  Novel events:        {self.stats['novel_events']}")
        logger.info(f"  Gate-tighten alerts: {self.stats['gate_tighten_alerts']}")
        logger.info(f"  Direction flips:     {self.stats['direction_changes']}")
        logger.info(f"  Suppressed (follow-ups): {self.stats['suppressed_alerts']}")
        logger.info(f"  Errors:              {self.stats['errors']}")

    def shutdown(self, signum=None, frame=None):
        logger.info("Shutdown signal received, finishing current cycle...")
        self.shutdown_requested = True


def main():
    pipeline = LivePipeline()

    # Graceful shutdown on Ctrl+C, docker stop, etc.
    signal.signal(signal.SIGINT, pipeline.shutdown)
    signal.signal(signal.SIGTERM, pipeline.shutdown)

    pipeline.run_forever()


if __name__ == "__main__":
    main()