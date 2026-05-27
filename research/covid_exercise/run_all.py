"""
COVID Exercise — Master Runbook
================================

Runs the full exercise end to end:
  1. Schema migration
  2. Topic registration
  3. Pull GDELT for COVID period (Nov 2019 - Apr 2020)
  4. Pull GDELT for control: Ebola 2014
  5. Pull GDELT for control: MERS 2015
  6. Pull market data for all periods
  7. Run retrospective surveillance
  8. Generate analysis + plots

Each step is idempotent — safe to re-run.

Usage:
    # Run everything
    python -m research.covid_exercise.run_all

    # Run only certain stages
    python -m research.covid_exercise.run_all --stages 6,7,8
"""

import os
import sys
import subprocess
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


STAGES = [
    {
        "n": 1,
        "name": "Schema migration",
        "cmd": [sys.executable, "-m", "research.covid_exercise.schema"],
    },
    {
        "n": 2,
        "name": "Topic registration",
        "cmd": [sys.executable, "-m", "research.covid_exercise.topics"],
    },
    {
        "n": 3,
        "name": "GDELT pull — COVID window",
        "cmd": [
            sys.executable, "-m", "research.covid_exercise.collect_gdelt",
            "--topic", "covid",
            "--keywords-from", "pandemic_signal",
            "--start", "2019-11-01",
            "--end", "2020-04-30",
        ],
    },
    {
        "n": 4,
        "name": "GDELT pull — Ebola 2014 control",
        "cmd": [
            sys.executable, "-m", "research.covid_exercise.collect_gdelt",
            "--topic", "ebola_2014",
            "--keywords-from", "pandemic_signal",
            "--start", "2014-07-01",
            "--end", "2015-01-31",
        ],
    },
    {
        "n": 5,
        "name": "GDELT pull — MERS 2015 control",
        "cmd": [
            sys.executable, "-m", "research.covid_exercise.collect_gdelt",
            "--topic", "mers_2015",
            "--keywords-from", "pandemic_signal",
            "--start", "2015-04-01",
            "--end", "2015-09-30",
        ],
    },
    {
        "n": 6,
        "name": "Market data — all periods",
        "cmd": [
            sys.executable, "-m", "research.covid_exercise.collect_markets",
            "--start", "2014-06-01",
            "--end", "2020-06-30",
        ],
    },
    {
        "n": 7,
        "name": "Retrospective surveillance — all topics",
        "cmd": None,  # multi-step
        "subcommands": [
            [
                sys.executable, "-m", "research.covid_exercise.simulate",
                "--topic", "covid", "--bdba-topic", "pandemic_signal",
                "--start", "2019-12-01", "--end", "2020-04-30",
            ],
            [
                sys.executable, "-m", "research.covid_exercise.simulate",
                "--topic", "ebola_2014", "--bdba-topic", "pandemic_signal",
                "--start", "2014-08-01", "--end", "2015-01-31",
            ],
            [
                sys.executable, "-m", "research.covid_exercise.simulate",
                "--topic", "mers_2015", "--bdba-topic", "pandemic_signal",
                "--start", "2015-05-01", "--end", "2015-09-30",
            ],
        ],
    },
    {
        "n": 8,
        "name": "Analysis + plots",
        "cmd": [sys.executable, "-m", "research.covid_exercise.analyze"],
    },
]


def run_stage(stage):
    log.info("=" * 70)
    log.info(f"STAGE {stage['n']}: {stage['name']}")
    log.info("=" * 70)
    
    if stage.get("subcommands"):
        for cmd in stage["subcommands"]:
            log.info(f"  > {' '.join(cmd[2:])}")
            r = subprocess.run(cmd, cwd=os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            if r.returncode != 0:
                log.error(f"Subcommand failed with code {r.returncode}")
                return False
    elif stage.get("cmd"):
        r = subprocess.run(stage["cmd"], cwd=os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        if r.returncode != 0:
            log.error(f"Stage failed with code {r.returncode}")
            return False
    
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stages", default="all",
                        help="Comma-separated stage numbers, or 'all'")
    args = parser.parse_args()
    
    if args.stages == "all":
        to_run = STAGES
    else:
        wanted = {int(s) for s in args.stages.split(",")}
        to_run = [s for s in STAGES if s["n"] in wanted]
    
    log.info(f"Running stages: {[s['n'] for s in to_run]}")
    
    for stage in to_run:
        ok = run_stage(stage)
        if not ok:
            log.error(f"Stopping at stage {stage['n']}")
            sys.exit(1)
    
    log.info("=" * 70)
    log.info("ALL STAGES COMPLETE")
    log.info("=" * 70)
    log.info("Results in /mnt/user-data/outputs/:")
    log.info("  - covid_analysis.md")
    log.info("  - bdba_covid_timeline.png")
    log.info("  - bdba_ebola_2014_timeline.png")
    log.info("  - bdba_mers_2015_timeline.png")


if __name__ == "__main__":
    main()
