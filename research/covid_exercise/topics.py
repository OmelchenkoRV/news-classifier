"""
BDBA topic definitions for the COVID exercise.

DESIGN PRINCIPLE: keywords must be defensible PRE-event. We're not
allowed to use 'COVID' or 'coronavirus' as keywords because we wouldn't
have known to look for them in November 2019.

The 'pandemic_signal' topic uses generic outbreak/disease language
that any BDBA system would reasonably include from day one. This is
the honest test.

Run once after schema:
    python -m research.covid_exercise.topics
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor


# ═══════════════════════════════════════════════════════════════════
# TOPIC DEFINITIONS
# ═══════════════════════════════════════════════════════════════════
# Each topic has primary keywords (any match = headline matches)
# and exclude keywords (filter out routine/seasonal noise).

TOPICS = [
    {
        "name": "pandemic_signal",
        "description": (
            "Generic disease outbreak / pandemic warning signals. "
            "Defensible PRE-COVID — language used in 2003 SARS, 2014 Ebola, "
            "2015 MERS, etc. No COVID-specific terms allowed."
        ),
        "primary_keywords": [
            # Outbreak language
            "outbreak", "epidemic", "pandemic",
            # Pathogen-specific (generic)
            "novel virus", "new virus", "unknown virus",
            "mystery illness", "mystery disease",
            # Pneumonia-specific (this was THE COVID warm-up signal)
            "pneumonia outbreak", "pneumonia cluster",
            "viral pneumonia",
            # Containment language
            "quarantine", "lockdown", "containment zone",
            "travel restriction", "travel ban",
            # Authority warnings
            "WHO emergency", "WHO warning",
            "CDC warning", "CDC alert",
            "public health emergency",
            # Spread
            "human-to-human transmission",
            "cluster of cases",
            "cases reported",
        ],
        "boost_keywords": [
            # Stronger signals that elevate the headline
            "WHO declares", "CDC declares",
            "international concern",
            "pandemic potential",
            "novel pathogen",
        ],
        "exclude_keywords": [
            # Routine seasonal flu and known endemic stuff
            "flu shot", "flu vaccine schedule",
            "seasonal flu",
            "common cold",
            # Drugs and treatments that might match outbreak words but aren't outbreaks
            "outbreak movie", "outbreak film",
            # Animal-only
            "bird flu vaccination",  # routine
        ],
        "baseline_window_days": 30,
        "alert_z_threshold": 3.0,
        "min_baseline_count": 2.0,
        "notes": (
            "PRE-COVID-defensible keywords. The Wuhan pneumonia cluster reports "
            "in late Dec 2019 and Jan 2020 should have triggered 'pneumonia "
            "outbreak' and 'pneumonia cluster' matches before any specific "
            "COVID keyword existed. This is the warm-up signal we're testing."
        ),
    },
]


def main():
    conn = get_connection()
    cur = get_cursor(conn)
    
    for topic in TOPICS:
        cur.execute("""
            INSERT INTO bdba_topics
                (name, description, primary_keywords, boost_keywords,
                 exclude_keywords, baseline_window_days, alert_z_threshold,
                 min_baseline_count, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                primary_keywords = EXCLUDED.primary_keywords,
                boost_keywords = EXCLUDED.boost_keywords,
                exclude_keywords = EXCLUDED.exclude_keywords,
                baseline_window_days = EXCLUDED.baseline_window_days,
                alert_z_threshold = EXCLUDED.alert_z_threshold,
                min_baseline_count = EXCLUDED.min_baseline_count,
                notes = EXCLUDED.notes
        """, (
            topic["name"],
            topic["description"],
            topic["primary_keywords"],
            topic.get("boost_keywords", []),
            topic.get("exclude_keywords", []),
            topic["baseline_window_days"],
            topic["alert_z_threshold"],
            topic["min_baseline_count"],
            topic.get("notes", ""),
        ))
        print(f"Topic registered: {topic['name']}")
        print(f"  Primary keywords: {len(topic['primary_keywords'])}")
        print(f"  Exclude keywords: {len(topic.get('exclude_keywords', []))}")
        print(f"  Z threshold: {topic['alert_z_threshold']}")
    
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
