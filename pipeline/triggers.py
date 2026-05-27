"""
Event Novelty Trigger Manager
================================

Core logic for the trigger system. Distinguishes novel events from
reactive/follow-up coverage.

Key insight: markets react to the FIRST appearance of a narrative.
  - "US bombs Iran" appears → creates trigger "us-iran-conflict" → market reacts
  - 500 follow-up articles about the same topic → NO additional market reaction
  - After 7 days → trigger fades, stops tightening gates
  - After 30 days → trigger archived, deleted from matching pool

Lifecycle states:
  ACTIVE (0-24h)   — novel event, fully tightens gates
  FADING (1-7d)    — still relevant but impact decays linearly
  STALE (7-30d)    — no longer affects gates, kept for matching
  ARCHIVED (30d+)  — removed from active matching

A "signature" is the canonical identifier. We use keyword sets because they
handle paraphrasing well: "USA bombing Iran" and "US strikes Iran" generate
similar keyword sets and match the same trigger.

Similarity metric: Jaccard coefficient on keyword sets.
  If 60% of keywords overlap with an existing ACTIVE trigger, it's the same event.
"""

import os
import sys
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# SIMILARITY AND SIGNATURE EXTRACTION
# ═══════════════════════════════════════════════════════════════════

# Words that should NOT count as signature keywords (too generic)
STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be',
    'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'can', 'of', 'in', 'on', 'at', 'to',
    'for', 'with', 'by', 'from', 'up', 'about', 'into', 'through', 'over',
    'after', 'before', 'as', 'than', 'this', 'that', 'these', 'those',
    'i', 'you', 'he', 'she', 'it', 'we', 'they', 'them', 'their', 'there',
    'what', 'which', 'who', 'when', 'where', 'why', 'how', 'all', 'each',
    'every', 'any', 'some', 'no', 'not', 'only', 'own', 'same', 'so', 'very',
    'just', 'also', 'new', 'now', 'then', 'says', 'said', 'say', 'told',
    'according', 'report', 'reports', 'reported', 'reportedly', 'amid',
    'plans', 'plan', 'over', 'around', 'despite', 'including', 'amid',
    's', 't', 'm', 'd', 've', 'll', 're',  # 'us' removed — handled as alias to 'usa'
}

# High-value named entities that should always be signature keywords if present
# These are the "anchor" concepts around which narratives form
ANCHOR_ENTITIES = {
    # Geopolitical actors (nations)
    'usa', 'britain', 'iran', 'russia', 'china', 'ukraine', 'israel',
    'gaza', 'lebanon', 'taiwan', 'north korea', 'south korea', 'japan',
    'india', 'turkey', 'saudi', 'venezuela',
    # International bodies
    'nato', 'eu', 'european union', 'un',
    # Political leaders (resolved to nations via aliases mostly)
    'trump', 'biden', 'putin',
    # Locations
    'hormuz', 'tehran', 'moscow', 'beijing', 'washington', 'brussels', 'kyiv',
    # Institutions
    'fed', 'federal reserve', 'fomc', 'ecb', 'boj', 'sec', 'doj', 'cftc',
    'treasury', 'irs', 'white house', 'pentagon',
    # Crypto entities
    'bitcoin', 'ethereum', 'binance', 'coinbase', 'kraken', 'tether',
    'usdc', 'usdt', 'microstrategy', 'blackrock', 'fidelity',
    # Event types (verbs that can be part of a pair key)
    'war', 'ceasefire', 'tariff', 'sanction', 'hack', 'exploit', 'etf',
    'rate cut', 'rate hike', 'inflation', 'cpi', 'recession',
    'upgrade', 'fork', 'bankruptcy', 'depeg',
    # Pre-escalation signals (often precede military/economic events)
    'evacuation', 'embassy',
}

# Aliases: map variant forms to canonical entity
ENTITY_ALIASES = {
    'federal reserve': 'fed',
    'fomc': 'fed',
    'powell': 'fed',
    'jerome powell': 'fed',
    'us': 'usa',
    'united states': 'usa',
    'america': 'usa',
    'american': 'usa',
    'uk': 'britain',
    'british': 'britain',
    'united kingdom': 'britain',
    'irgc': 'iran',
    'khamenei': 'iran',
    'ayatollah': 'iran',
    'iranian': 'iran',
    'hezbollah': 'lebanon',
    'lebanese': 'lebanon',
    'idf': 'israel',
    'israeli': 'israel',
    'netanyahu': 'israel',
    'palestinian': 'gaza',
    'tehran': 'iran',
    'beijing': 'china',
    'chinese': 'china',
    'moscow': 'russia',
    'russian': 'russia',
    'kyiv': 'ukraine',
    'ukrainian': 'ukraine',
    'zelensky': 'ukraine',
    'putin': 'russia',
    'xi': 'china',
    'north korean': 'north korea',
    'south korean': 'south korea',
    'japanese': 'japan',
    'indian': 'india',
    'turkish': 'turkey',
    'saudi arabia': 'saudi',
    'saudis': 'saudi',
    'venezuelan': 'venezuela',
    'european': 'eu',
    'strait of hormuz': 'hormuz',
    'european central bank': 'ecb',
    'bank of japan': 'boj',
    'securities and exchange commission': 'sec',
    'department of justice': 'doj',
    'white house': 'usa',
    'btc': 'bitcoin',
    'eth': 'ethereum',
    # Pre-escalation action aliases
    'evacuate': 'evacuation',
    'evacuations': 'evacuation',
    'evacuating': 'evacuation',
    'embassies': 'embassy',
}

# Minimum similarity to match an existing trigger (using hybrid similarity)
# 0.4 = at least one shared anchor entity with related context
# Lower values cause false merges (different events conflated)
# Higher values fragment the same narrative into multiple triggers
SIMILARITY_THRESHOLD = 0.4

# Core anchors are entities that, when shared between two headlines,
# strongly suggest they're about the same narrative. These are the
# major geopolitical actors and key institutions whose mention is
# very specific (unlike "war" or "ceasefire" which appear in many
# unrelated stories).
#
# Example: if both headlines mention "iran", they're very likely
# the same Iran narrative even if other details differ.
CORE_ANCHORS = {
    'iran', 'russia', 'ukraine', 'israel', 'china', 'gaza', 'lebanon',
    'taiwan', 'north korea', 'venezuela',
    'fed', 'sec', 'ecb',
    'bitcoin', 'ethereum',
}

# When two triggers share a core anchor, use a lower threshold for matching.
# This is the key fix for trigger fragmentation: 'iran-usa' and
# 'iran-trump-usa' should merge because both have core anchor 'iran'.
CORE_ANCHOR_THRESHOLD = 0.25

# Age thresholds for state transitions
ACTIVE_HOURS = 24       # 0-24h = ACTIVE
FADING_DAYS = 7         # 1-7d = FADING
STALE_DAYS = 30         # 7-30d = STALE, 30+ = ARCHIVED


# Entity types — determines how they combine into relational pairs
ENTITY_TYPES = {
    # Nations / state actors (form pairs in conflicts)
    'usa': 'nation', 'iran': 'nation', 'russia': 'nation',
    'china': 'nation', 'ukraine': 'nation', 'israel': 'nation',
    'gaza': 'nation', 'lebanon': 'nation', 'taiwan': 'nation',
    'north korea': 'nation', 'south korea': 'nation', 'britain': 'nation',
    'japan': 'nation', 'india': 'nation', 'turkey': 'nation',
    'saudi': 'nation', 'venezuela': 'nation',
    # Political leaders act as their nation's representative
    'trump': 'nation', 'biden': 'nation', 'putin': 'nation',

    # Regulatory/monetary institutions
    'fed': 'institution', 'ecb': 'institution', 'boj': 'institution',
    'fomc': 'institution', 'sec': 'institution', 'cftc': 'institution',
    'doj': 'institution', 'treasury': 'institution', 'irs': 'institution',
    'nato': 'institution', 'eu': 'institution', 'european union': 'institution',
    'un': 'institution', 'white house': 'institution', 'pentagon': 'institution',

    # Crypto entities (targets of regulatory/exchange events)
    'bitcoin': 'crypto', 'ethereum': 'crypto',
    'binance': 'crypto', 'coinbase': 'crypto', 'kraken': 'crypto',
    'tether': 'crypto', 'usdc': 'crypto', 'usdt': 'crypto',
    'microstrategy': 'crypto', 'blackrock': 'crypto', 'fidelity': 'crypto',

    # Event types (verbs that can be part of a pair key)
    'war': 'action', 'ceasefire': 'action', 'tariff': 'action',
    'sanction': 'action', 'hack': 'action', 'exploit': 'action',
    'etf': 'action', 'rate cut': 'action', 'rate hike': 'action',
    'inflation': 'action', 'cpi': 'action', 'recession': 'action',
    'upgrade': 'action', 'fork': 'action', 'bankruptcy': 'action',
    'depeg': 'action',
    # Pre-escalation signals
    'evacuation': 'action', 'embassy': 'action',

    # Locations can anchor geographic events
    'hormuz': 'location', 'tehran': 'location', 'moscow': 'location',
    'beijing': 'location', 'washington': 'location', 'brussels': 'location',
    'kyiv': 'location',
}


def extract_actor_pairs(anchors: set) -> set:
    """
    Build relational pairs from anchors.

    When 2+ nations appear → nation-nation pair (conflict/diplomacy)
    When nation + action   → nation-action pair (policy event)
    When institution + action → inst-action pair (monetary/regulatory)
    When 2+ institutions   → inst-inst pair

    Pairs are sorted alphabetically — `iran-usa` == `usa-iran`.
    This loses direction (who attacked whom) but gains recall:
    "US strikes Iran" and "Iran retaliates against US" match the same conflict.
    That's usually what we want for event tracking — both are the same war.

    Returns a set of "a+b" strings (sorted pairs).
    """
    pairs = set()

    nations = [a for a in anchors if ENTITY_TYPES.get(a) == 'nation']
    institutions = [a for a in anchors if ENTITY_TYPES.get(a) == 'institution']
    actions = [a for a in anchors if ENTITY_TYPES.get(a) == 'action']
    cryptos = [a for a in anchors if ENTITY_TYPES.get(a) == 'crypto']
    locations = [a for a in anchors if ENTITY_TYPES.get(a) == 'location']

    # Nation-nation pairs (conflicts)
    for i, n1 in enumerate(nations):
        for n2 in nations[i + 1:]:
            pairs.add("+".join(sorted([n1, n2])))

    # Nation-action pairs (e.g. "trump+tariff", "china+sanction")
    for n in nations:
        for a in actions:
            pairs.add("+".join(sorted([n, a])))

    # Institution-action pairs (e.g. "fed+rate_cut", "sec+etf")
    for inst in institutions:
        for a in actions:
            pairs.add("+".join(sorted([inst, a.replace(" ", "_")])))

    # Institution-crypto pairs (e.g. "sec+bitcoin")
    for inst in institutions:
        for c in cryptos:
            pairs.add("+".join(sorted([inst, c])))

    # Nation-location pairs (e.g. "iran+hormuz")
    for n in nations:
        for loc in locations:
            pairs.add("+".join(sorted([n, loc])))

    return pairs


def extract_keywords(title: str) -> tuple[set, set, set]:
    """
    Extract (keywords, anchor_entities, actor_pairs) from a headline.

    Returns:
        keywords: all significant words (used for fine-grained similarity)
        anchors: canonical named entities (who/what is mentioned)
        pairs: relational pairs (iran+usa, fed+rate_cut, etc.)
    """
    # Normalize: lowercase, strip punctuation
    text = re.sub(r"[^\w\s]", " ", title.lower())
    words = text.split()

    keywords = set()
    anchors = set()

    text_lower = " " + text + " "

    # Multi-word aliases first (canonicalize variants)
    for alias, canonical in ENTITY_ALIASES.items():
        if len(alias.split()) > 1 and f" {alias} " in text_lower:
            anchors.add(canonical)
            keywords.add(canonical)

    # Multi-word anchors (e.g. "federal reserve", "north korea")
    for entity in ANCHOR_ENTITIES:
        if len(entity.split()) > 1 and f" {entity} " in text_lower:
            anchors.add(entity)
            keywords.add(entity.replace(" ", "_"))

    # Single-word processing
    for w in words:
        # Check alias BEFORE length filter so short tokens like "us"→"usa" work
        canonical = ENTITY_ALIASES.get(w, w)
        if canonical in ANCHOR_ENTITIES:
            anchors.add(canonical)
            keywords.add(canonical)
            continue
        # Otherwise apply length/stopword filters
        if len(w) < 3 or w in STOPWORDS:
            continue
        if w.isdigit():
            continue
        keywords.add(w)

    # Build relational pairs from anchors
    pairs = extract_actor_pairs(anchors)

    return keywords, anchors, pairs


def hybrid_similarity(
    kw_a: set, anchors_a: set, pairs_a: set,
    kw_b: set, anchors_b: set, pairs_b: set,
) -> float:
    """
    Three-tier similarity:
      1. Actor pairs (highest weight — relational structure)
      2. Anchor entities (who is mentioned)
      3. Keywords (fine-grained context)

    Weights: 50% pairs + 30% anchors + 20% keywords (when pairs exist).
    Falls back to 70% anchors + 30% keywords when no pairs.
    Falls back to pure keyword similarity when no anchors either.

    Why pairs dominate: "US strikes Iran" and "Iran retaliates against US"
    share the pair `iran+usa` even if worded very differently. Matching on
    pairs catches the same conflict across coverage angles.
    """
    pair_sim = 0.0
    if pairs_a and pairs_b:
        pair_sim = len(pairs_a & pairs_b) / len(pairs_a | pairs_b)

    anchor_sim = 0.0
    if anchors_a and anchors_b:
        anchor_sim = len(anchors_a & anchors_b) / len(anchors_a | anchors_b)

    kw_sim = 0.0
    if kw_a and kw_b:
        kw_sim = len(kw_a & kw_b) / len(kw_a | kw_b)

    # Weighted combination depending on what signals are available
    if pairs_a or pairs_b:
        return 0.5 * pair_sim + 0.3 * anchor_sim + 0.2 * kw_sim
    if anchors_a or anchors_b:
        return 0.7 * anchor_sim + 0.3 * kw_sim
    return kw_sim


def build_signature(
    anchors: set,
    keywords: set,
    direction: Optional[str] = None,
) -> str:
    """
    Build canonical trigger signature from anchor entities.

    `direction`, if a strong value ('escalation' or 'de-escalation'), is
    appended as a suffix so that opposite-direction narratives form
    distinct triggers. Other direction values (None, 'neutral',
    'context-dependent', 'unknown') do not affect the signature.

    Examples:
        anchors={iran, hormuz}, direction='escalation'
            → "hormuz-iran-escalation"
        anchors={iran, hormuz}, direction='de-escalation'
            → "hormuz-iran-de-escalation"
        anchors={iran, hormuz}, direction=None
            → "hormuz-iran"
        anchors={fed, rate cut}          → "fed-rate_cut"
        anchors={} keywords={defi, kelp} → "defi-kelp"  (fallback)
    """
    if anchors:
        # Use anchors, sorted for consistency
        parts = sorted(a.replace(" ", "_") for a in anchors)
    else:
        # Fallback: top 3 keywords by length (longer = more specific)
        parts = sorted(keywords, key=len, reverse=True)[:3]

    base = "-".join(parts[:4])  # cap at 4 parts
    return base + _direction_suffix(direction)


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """
    Similarity between two keyword sets.

    Returns value in [0, 1]:
        0   = no overlap
        1   = identical
        0.6 = our threshold for "same event"
    """
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════
# DIRECTIONAL MATCHING (Stage 2 integration)
# ═══════════════════════════════════════════════════════════════════
#
# Direction values that are produced by VerbExtractor:
#   'escalation' / 'de-escalation' — strong directional signal
#   'neutral'                       — verb itself doesn't carry direction
#   'context-dependent'             — direction depends on object semantics
#   'unknown'                       — verb not in taxonomy / no verb found
#   None                            — direction not provided (legacy callers)
#
# Only escalation/de-escalation are STRONG signals. The other three are
# treated as "unspecified" for matching purposes — they neither force
# separation nor block merging.

_STRONG_DIRECTIONS = {'escalation', 'de-escalation'}

# When two strong directions are opposite, triggers must NOT merge.
_OPPOSITE_PAIRS = {
    frozenset({'escalation', 'de-escalation'}),
}


def directions_are_opposite(a: Optional[str], b: Optional[str]) -> bool:
    """True iff `a` and `b` are both strong directions and they conflict.

    Any combination involving None / 'neutral' / 'context-dependent' /
    'unknown' returns False — those cases must be allowed to match either
    way so unclassified follow-up coverage attaches to the correct
    directional sibling rather than fragmenting.
    """
    if a not in _STRONG_DIRECTIONS or b not in _STRONG_DIRECTIONS:
        return False
    return frozenset({a, b}) in _OPPOSITE_PAIRS


def _direction_suffix(direction: Optional[str]) -> str:
    """Suffix appended to trigger signatures to separate directional siblings.

    Only strong directions get a suffix. Unspecified directions get no
    suffix, so legacy / unclassified triggers continue to use the bare
    anchor-based signature and absorb new headlines whose direction is
    also unspecified.

    Examples:
        direction='escalation'        -> '-escalation'
        direction='de-escalation'     -> '-de-escalation'
        direction='neutral'           -> ''
        direction='unknown' / None    -> ''
    """
    if direction in _STRONG_DIRECTIONS:
        return f"-{direction}"
    return ""


# ═══════════════════════════════════════════════════════════════════
# IMPACT SCORE DECAY
# ═══════════════════════════════════════════════════════════════════

def compute_impact_score(first_seen_at: datetime, now: datetime = None) -> float:
    """
    Decay impact score based on age of first appearance.

    Curve:
        0-2h:    1.0    (maximum impact)
        2-24h:   1.0 → 0.7 (linear)
        1-3d:    0.7 → 0.3 (linear, fading)
        3-7d:    0.3 → 0.1 (linear, mostly priced in)
        7d+:     0.0    (stale, no impact)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    age_hours = (now - first_seen_at).total_seconds() / 3600

    if age_hours <= 2:
        return 1.0
    elif age_hours <= 24:
        # Linear 1.0 → 0.7 over 22h
        return 1.0 - ((age_hours - 2) / 22) * 0.3
    elif age_hours <= 72:
        # Linear 0.7 → 0.3 over 48h
        return 0.7 - ((age_hours - 24) / 48) * 0.4
    elif age_hours <= 168:  # 7 days
        # Linear 0.3 → 0.1 over 96h
        return 0.3 - ((age_hours - 72) / 96) * 0.2
    else:
        return 0.0


def compute_state(first_seen_at: datetime, now: datetime = None) -> str:
    """Return state based on age from first sighting."""
    if now is None:
        now = datetime.now(timezone.utc)
    age = now - first_seen_at

    if age < timedelta(hours=ACTIVE_HOURS):
        return 'ACTIVE'
    elif age < timedelta(days=FADING_DAYS):
        return 'FADING'
    elif age < timedelta(days=STALE_DAYS):
        return 'STALE'
    else:
        return 'ARCHIVED'


# ═══════════════════════════════════════════════════════════════════
# TRIGGER MANAGER — main interface
# ═══════════════════════════════════════════════════════════════════

class TriggerManager:
    """
    Core interface for trigger operations.

    Usage:
        tm = TriggerManager()
        result = tm.process_headline(headline_id, title, category)
        # result = {
        #   'trigger_id': 42,
        #   'is_novel': True/False,  # was this the headline that created the trigger?
        #   'signature': 'iran-hormuz-conflict',
        #   'state': 'ACTIVE',
        #   'impact_score': 0.85,
        #   'mention_count': 1,
        # }
    """

    def __init__(self):
        # Categories that use triggers (others don't — they're always novel
        # enough to react to, like exchange hacks)
        self.tracked_categories = {
            'geopolitical', 'regulatory', 'macro',
        }

    def process_headline(
        self, headline_id: int, title: str, category: str,
        conn=None,
        direction: Optional[str] = None,
        verb_category: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[dict]:
        """
        Main entry point. Given a new headline, determine if it's a novel
        event or a follow-up to an existing trigger.

        Args:
            headline_id: DB id of the headline being processed.
            title: headline text (used for signature extraction and display).
            category: classifier category — only certain categories track triggers.
            conn: optional DB connection; new one is opened/closed if absent.
            direction: optional Stage 2 directional label
                ('escalation' / 'de-escalation' / 'neutral' /
                'context-dependent' / 'unknown' / None).
                Two triggers with the same anchors but OPPOSITE strong
                directions (escalation vs de-escalation) will not merge.
                Other values do not affect matching.
            verb_category: optional Stage 2 verb category (e.g. 'CLOSES').
                Recorded on the mention; updates the trigger's
                `dominant_verb_category` to the most recent mention's
                category when matching an existing trigger.
            now: optional override for "current time". When None
                (the default, used by all live callers), the function
                uses NOW()/datetime.now() as before — unchanged behaviour.
                When set (used by the historical replay in Phase 5c),
                all timestamps written to the database (last_seen_at,
                first_seen_at, mention.published_at_processing) and the
                Python-level state/impact computations use this value
                instead of wall-clock time. This lets replay walk
                headlines chronologically using their own published_at
                so the trigger state machine produces the same output
                it would have produced live.
                Must be timezone-aware if provided.

        Returns None if the headline's category doesn't use triggers.

        Returned dict (when not None) includes:
            trigger_id, is_novel, signature, display_name, state,
            impact_score, mention_count, similarity, direction (the
            trigger's own direction), and is_direction_change (True iff
            this mention differs from the trigger's recorded direction
            in a strong way).
        """
        if category not in self.tracked_categories:
            return None

        # Resolve the clock once at the top so all downstream code uses
        # the same "now". For live callers, this captures wall-clock at
        # entry; for replay, it adopts the headline's published_at.
        if now is not None and now.tzinfo is None:
            raise ValueError(
                f"now must be timezone-aware; got naive {now!r}"
            )
        clock_now = now if now is not None else datetime.now(timezone.utc)
        # Whether to override SQL NOW() with the parameterised clock.
        # When False (live mode), SQL keeps using NOW() — identical to
        # pre-Phase-5c behaviour. When True (replay), we substitute %s
        # bound to clock_now everywhere.
        replay_mode = now is not None

        keywords, anchors, pairs = extract_keywords(title)

        # Need at least 1 anchor OR 3+ keywords to form a trigger
        # Otherwise headline is too generic to track
        if len(anchors) < 1 and len(keywords) < 3:
            return None

        owned_conn = conn is None
        if owned_conn:
            conn = get_connection()

        try:
            cur = get_cursor(conn)

            # Find candidate triggers: same category, not archived
            # Also pull `direction` and `dominant_verb_category` so we can
            # skip directionally-conflicting candidates and report the
            # trigger's direction back to the caller.
            # Candidate fetch: parameterise NOW() so replay can fix
            # the clock to the headline's published_at. In live mode
            # (clock_now == wall-clock-at-entry, replay_mode == False),
            # passing clock_now to %s is equivalent to using NOW()
            # except that NOW() would re-evaluate at SQL-execution time
            # (microseconds later, irrelevant to the 30-day window).
            cur.execute("""
                SELECT id, signature, display_name, keywords, entities,
                       actor_pairs, first_seen_at, state, mention_count,
                       direction, dominant_verb_category
                FROM triggers
                WHERE category = %s
                  AND state != 'ARCHIVED'
                  AND last_seen_at > %s - INTERVAL '30 days'
            """, (category, clock_now))
            candidates = cur.fetchall()

            # Find best match using three-tier similarity (pairs-weighted)
            # ── Hierarchical merging: ──────────────────────────────
            # If the new headline and a candidate trigger share a CORE
            # anchor (iran, fed, etc.), use a lower threshold (0.25).
            # This prevents fragmentation like 'iran-usa' + 'iran-trump-usa'
            # + 'iran-pentagon-trump' all being separate triggers when
            # they're all clearly the same Iran narrative.
            new_core_anchors = anchors & CORE_ANCHORS

            best_match = None
            best_similarity = 0.0
            best_threshold = SIMILARITY_THRESHOLD

            for cand in candidates:
                # ── Directional conflict check (Stage 2) ──────────
                # If the new headline has a STRONG direction and the
                # candidate trigger also has a STRONG direction, and
                # they're opposite, skip — these belong to separate
                # triggers (e.g. Iran-Hormuz escalation vs de-escalation).
                # If either side's direction is unspecified / neutral /
                # context-dependent / unknown, we don't block matching.
                if directions_are_opposite(direction, cand.get('direction')):
                    continue

                cand_keywords = set(cand['keywords'])
                cand_anchors = set(cand['entities'] or [])
                cand_pairs = set(cand['actor_pairs'] or [])
                sim = hybrid_similarity(
                    keywords, anchors, pairs,
                    cand_keywords, cand_anchors, cand_pairs,
                )

                # Determine threshold: lower if shared core anchor
                shared_core = new_core_anchors & cand_anchors
                threshold = CORE_ANCHOR_THRESHOLD if shared_core else SIMILARITY_THRESHOLD

                # Pick best match that meets its threshold
                # Prefer a match that meets a lower threshold over one that
                # only meets the strict threshold by a small margin —
                # we want to absorb headlines into the existing narrative
                if sim >= threshold and sim > best_similarity:
                    best_similarity = sim
                    best_match = cand
                    best_threshold = threshold

            # clock_now was resolved at function entry (live=wall-clock,
            # replay=headline's published_at). Reuse it here so all
            # downstream state/impact computations align.
            now = clock_now

            if best_match and best_similarity >= best_threshold:
                # ── UPDATE existing trigger ────────────────────────
                trigger_id = best_match['id']
                signature = best_match['signature']
                first_seen = best_match['first_seen_at']
                new_state = compute_state(first_seen, now)
                new_impact = compute_impact_score(first_seen, now)

                # Merge new anchors/keywords/pairs into existing trigger
                # so future slightly-different phrasings match better
                merged_keywords = list(set(best_match['keywords']) | keywords)
                existing_entities = set(best_match.get('entities') or [])
                merged_entities = list(existing_entities | anchors)
                existing_pairs = set(best_match.get('actor_pairs') or [])
                merged_pairs = list(existing_pairs | pairs)

                # Direction-change detection: this mention's direction
                # differs from the trigger's recorded direction in a
                # strong way. Used by the alerter to surface "trigger
                # flipped direction" as elevated severity.
                trigger_direction = best_match.get('direction')
                is_direction_change = (
                    direction in _STRONG_DIRECTIONS
                    and trigger_direction in _STRONG_DIRECTIONS
                    and direction != trigger_direction
                )
                # Note: the directions_are_opposite check above already
                # filtered out OPPOSITE strong directions. So in practice
                # is_direction_change is False here (because we'd have
                # skipped this candidate). We compute it anyway to keep
                # the contract honest in case future similarity rules
                # admit non-strict direction conflicts (e.g. escalation
                # vs neutral with high similarity override).

                # Update dominant_verb_category: most-recent-wins when a
                # category is provided. This is intentionally simple — a
                # true majority calc would need a separate aggregation
                # query over trigger_mentions, deferred until we have
                # evidence the simple rule is wrong.
                new_dominant_category = best_match.get('dominant_verb_category')
                if verb_category and verb_category != 'unknown':
                    new_dominant_category = verb_category

                # Backfill the trigger's direction if it was previously
                # NULL/unknown and this mention has a strong direction.
                # This lets pre-Stage-2 triggers acquire direction once
                # classified headlines start flowing in.
                new_trigger_direction = trigger_direction
                if (trigger_direction not in _STRONG_DIRECTIONS
                        and direction in _STRONG_DIRECTIONS):
                    new_trigger_direction = direction

                cur.execute("""
                    UPDATE triggers
                    SET last_seen_at = %s,
                        mention_count = mention_count + 1,
                        state = %s,
                        impact_score = %s,
                        keywords = %s,
                        entities = %s,
                        actor_pairs = %s,
                        direction = %s,
                        dominant_verb_category = %s
                    WHERE id = %s
                """, (clock_now, new_state, new_impact, merged_keywords,
                      merged_entities, merged_pairs, new_trigger_direction,
                      new_dominant_category, trigger_id))

                cur.execute("""
                    INSERT INTO trigger_mentions
                        (trigger_id, headline_id, caused_refresh,
                         similarity_score, direction, verb_category)
                    VALUES (%s, %s, TRUE, %s, %s, %s)
                    ON CONFLICT (trigger_id, headline_id) DO NOTHING
                """, (trigger_id, headline_id, best_similarity,
                      direction, verb_category))

                if owned_conn:
                    conn.commit()

                return {
                    'trigger_id': trigger_id,
                    'is_novel': False,
                    'signature': signature,
                    'display_name': best_match['display_name'],
                    'state': new_state,
                    'impact_score': new_impact,
                    'mention_count': best_match['mention_count'] + 1,
                    'similarity': best_similarity,
                    'direction': new_trigger_direction,
                    'is_direction_change': is_direction_change,
                }

            # ── CREATE new trigger ─────────────────────────────────
            # Signature includes a direction suffix when direction is
            # a strong value. This makes 'hormuz-iran-escalation' and
            # 'hormuz-iran-de-escalation' coexist as separate triggers.
            signature = build_signature(anchors, keywords, direction=direction)
            display_name = title[:120]

            # Only persist verb_category on the trigger if it's a real
            # category (not 'unknown'). Same for direction — we store
            # whatever was passed, including None / 'unknown', so the
            # trigger row faithfully reflects what we knew at creation.
            initial_dominant_category = (
                verb_category if verb_category and verb_category != 'unknown'
                else None
            )

            # New-trigger INSERT. We always explicitly set first_seen_at
            # and last_seen_at to clock_now (live: wall-clock at entry;
            # replay: headline's published_at). For live mode this is
            # equivalent to the schema's NOW() default; for replay it's
            # essential — otherwise replayed triggers all stamp "today"
            # for first_seen_at and the decay/state machinery breaks.
            cur.execute("""
                INSERT INTO triggers
                    (signature, display_name, category,
                     keywords, entities, actor_pairs,
                     first_headline_id, first_title,
                     state, impact_score, mention_count,
                     direction, dominant_verb_category,
                     first_seen_at, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        'ACTIVE', 1.0, 1, %s, %s, %s, %s)
                ON CONFLICT (signature) DO UPDATE SET
                    last_seen_at = %s,
                    mention_count = triggers.mention_count + 1
                RETURNING id, signature, state, impact_score, mention_count,
                          direction
            """, (
                signature, display_name, category,
                list(keywords), list(anchors), list(pairs),
                headline_id, title,
                direction, initial_dominant_category,
                clock_now, clock_now,   # first_seen_at, last_seen_at
                clock_now,              # ON CONFLICT update value
            ))
            result = cur.fetchone()
            trigger_id = result['id']

            cur.execute("""
                INSERT INTO trigger_mentions
                    (trigger_id, headline_id, caused_creation,
                     similarity_score, direction, verb_category)
                VALUES (%s, %s, TRUE, 1.0, %s, %s)
                ON CONFLICT (trigger_id, headline_id) DO NOTHING
            """, (trigger_id, headline_id, direction, verb_category))

            if owned_conn:
                conn.commit()

            return {
                'trigger_id': trigger_id,
                'is_novel': True,
                'signature': result['signature'],
                'display_name': display_name,
                'state': result['state'],
                'impact_score': result['impact_score'],
                'mention_count': result['mention_count'],
                'similarity': 1.0,
                'direction': result['direction'],
                'is_direction_change': False,  # creating == no prior state
            }

        finally:
            if owned_conn:
                conn.close()

    def decay_triggers(self, conn=None) -> dict:
        """
        Periodic maintenance: update all triggers' states and impact scores
        based on age. Should be called every hour or so.
        """
        owned_conn = conn is None
        if owned_conn:
            conn = get_connection()

        try:
            cur = get_cursor(conn)
            stats = {'active': 0, 'fading': 0, 'stale': 0, 'archived': 0}

            cur.execute("""
                SELECT id, first_seen_at, state
                FROM triggers
                WHERE state != 'ARCHIVED'
            """)
            rows = cur.fetchall()

            now = datetime.now(timezone.utc)
            for row in rows:
                new_state = compute_state(row['first_seen_at'], now)
                new_impact = compute_impact_score(row['first_seen_at'], now)

                cur.execute("""
                    UPDATE triggers
                    SET state = %s, impact_score = %s
                    WHERE id = %s
                """, (new_state, new_impact, row['id']))

                stats[new_state.lower()] = stats.get(new_state.lower(), 0) + 1

            if owned_conn:
                conn.commit()
            return stats

        finally:
            if owned_conn:
                conn.close()


# ═══════════════════════════════════════════════════════════════════
# CLI FOR TESTING
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Trigger manager utility")
    sub = parser.add_subparsers(dest="cmd")

    list_cmd = sub.add_parser("list", help="List active triggers")
    decay_cmd = sub.add_parser("decay", help="Run trigger decay/cleanup")
    test_cmd = sub.add_parser("test", help="Test signature extraction")
    test_cmd.add_argument("title", type=str, help="Headline to analyze")
    test_cmd.add_argument(
        "--direction", type=str, default=None,
        choices=['escalation', 'de-escalation', 'neutral',
                 'context-dependent', 'unknown'],
        help="Optional direction (Stage 2 output) to include in signature.",
    )

    args = parser.parse_args()

    if args.cmd == "test":
        keywords, anchors, pairs = extract_keywords(args.title)
        signature = build_signature(anchors, keywords, direction=args.direction)
        print(f"\nHeadline:  {args.title}")
        print(f"Direction: {args.direction or '(none)'}")
        print(f"Anchors:   {sorted(anchors)}")
        print(f"Pairs:     {sorted(pairs)}")
        print(f"Keywords:  {sorted(keywords)}")
        print(f"Signature: {signature}")

    elif args.cmd == "list":
        conn = get_connection()
        cur = get_cursor(conn)
        cur.execute("""
            SELECT signature, display_name, category, state,
                   impact_score, mention_count, 
                   first_seen_at, last_seen_at
            FROM triggers
            WHERE state IN ('ACTIVE', 'FADING')
            ORDER BY impact_score DESC, mention_count DESC
            LIMIT 30
        """)
        print(f"\n{'Signature':35s} {'State':9s} {'Score':5s} {'Mentions':8s} {'Age':8s}")
        print("-" * 100)
        for r in cur.fetchall():
            age = datetime.now(timezone.utc) - r['first_seen_at']
            age_str = f"{age.total_seconds()/3600:.1f}h"
            print(f"{r['signature'][:34]:35s} {r['state']:9s} {r['impact_score']:5.2f} "
                  f"{r['mention_count']:>8d} {age_str:>8s}  {r['display_name'][:60]}")
        conn.close()

    elif args.cmd == "decay":
        tm = TriggerManager()
        stats = tm.decay_triggers()
        print(f"Decay complete: {stats}")
