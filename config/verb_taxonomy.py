"""
Verb taxonomy for directional classification of news headlines.

Maps verb lemmas to broad action categories, and categories to directional
implications (escalation / de-escalation / neutral / context-dependent).

This is the static knowledge base that VerbExtractor consults after
parsing a headline. It is intentionally small and hand-curated so it can
live in version control and be reasoned about as a unit.

Per-category schema:
    canonical:        prototype verbs that most clearly express the action
    synonyms:         verbs that map to the same action with similar
                      directional load
    ambiguous_lemmas: lemmas that are valid taxonomy verbs but commonly
                      appear as nouns ("target" the goal vs "to target",
                      "place" the location vs "to place"). These match
                      ONLY when the parser confirms POS=VERB or AUX —
                      they are excluded from the lemma fallback that
                      catches POS-mistag cases. Empty list / missing key
                      means no entries.
    direction:        'escalation' | 'de-escalation' | 'neutral' |
                      'context-dependent'
    weight:           0..1. Reserved for future scoring; not used yet.

Adding a new verb: pick the most appropriate category, add the lemma to
canonical, synonyms, or ambiguous_lemmas. If no existing category fits,
add a new one — but prefer broadening synonyms over fragmenting categories.

The ambiguous_lemmas slot should be used when the noun form of the verb
is at least as common as the verb form in news headlines. Test: can you
think of a common headline where this lemma appears as a noun and would
NOT carry directional signal? If yes, put it in ambiguous_lemmas.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------
# Each category has:
#   canonical: prototype verbs that most clearly express the action
#   synonyms:  verbs that map to the same action with similar directional load
#   direction: 'escalation' | 'de-escalation' | 'neutral' | 'context-dependent'
#   weight:    0..1, how strongly the category implies its direction.
#              Reserved for future scoring / forecaster integration; not used
#              by the extractor yet.
#
# All verbs are stored as lowercase lemmas. Lookup is by lemma, not surface
# form, so spaCy's lemmatizer must run before classify_verb.

VERB_CATEGORIES: dict[str, dict] = {
    "ATTACKS": {
        "canonical": ["attack", "strike", "bomb", "fire", "launch"],
        "synonyms": [
            "threaten", "intimidate", "warn", "menace", "challenge",
            # Added 2026-04-29 from real headline data:
            "sue",      # "SEC sues exchange" — regulatory aggression
            # Added 2026-04-29 (round 2) from gates-only diagnose:
            "kill",         # 28 occurrences — direct violence
            "disrupt",      # 18 — "disrupts smuggling/operations"
            "accuse",       # 13 — "accuses of violations"
            "escalate",     # 31 — meta-verb naming the direction itself
            "intensify",    # not in top 40 but clearly directional
            "retaliate",    # not in top 40 but clearly directional
            "breach",       # treaty/agreement-breaking
            "violate",      # treaty/agreement-breaking
            # Removed 2026-04-29 per "primary-school-kid principle":
            # "claim" — too context-dependent ("claims responsibility"
            #           vs "claims success" vs "claims control")
        ],
        # Verb lemmas with common noun forms — only match when the
        # parser confirms POS=VERB/AUX (skipped in lemma fallback).
        "ambiguous_lemmas": [
            "charge",   # "Charges company with fraud" (verb) vs
                        # "electric charge", "the charge" (noun)
        ],
        "direction": "escalation",
        "weight": 1.0,
    },
    "DEFENDS": {
        "canonical": ["defend", "protect", "guard", "shield"],
        "synonyms": [
            "fortify", "secure", "reinforce",
            # Added 2026-04-29 (round 2):
            "deploy",       # 15 — "deploys troops/divisions" (readiness)
        ],
        # Implies a threat exists — defensive posture is itself an escalation
        # signal in geopolitical context.
        "direction": "escalation",
        "weight": 0.6,
    },
    "OPENS": {
        "canonical": ["open", "reopen", "restore", "resume"],
        # NB: "lift" lives in LIFTS — that's the dominant news usage
        # (lift sanctions, lift restrictions). Don't duplicate.
        "synonyms": [
            "release", "free", "ease",
            # Added 2026-04-29 from real headline data:
            "clear",    # "Iran clearing sea mines" — removing obstruction
        ],
        "direction": "de-escalation",
        "weight": 1.0,
    },
    "CLOSES": {
        "canonical": ["close", "shut", "block", "halt", "suspend"],
        "synonyms": ["restrict", "ban", "blockade"],
        # "cut off" is a phrasal verb — handled separately if we add phrasal
        # support; for now "cut" alone is too ambiguous to include.
        "direction": "escalation",
        "weight": 1.0,
    },
    "ANNOUNCES": {
        "canonical": ["announce", "declare", "confirm", "approve"],
        "synonyms": [
            "proclaim", "reveal", "disclose",
            # Added 2026-04-29: reporting verbs from real headline data.
            # These carry no direction themselves — the *content* of what
            # is reported carries direction. Mapping to ANNOUNCES (neutral)
            # means the trigger system absorbs them without forcing a
            # directional split, which is the right behaviour: Stage 3
            # NLI is responsible for inferring direction from content.
            "say",       # 157 occurrences in 5k headlines — primarily
                         # verb in headlines, "says" rarely a noun.
            "show",      #  31 occurrences
        ],
        # Verb lemmas with common noun forms — only match when POS-confirmed:
        "ambiguous_lemmas": [
            "report",    #  23 — "Annual Report", "according to the report"
            "signal",    #  25 — "buy signal", "trading signal", "the signal"
            "note",      # "research note", "policy note", "a note from..."
        ],
        # The act of announcing is itself directionless; what's announced
        # carries the signal. Stage 3 NLI is expected to disambiguate.
        "direction": "neutral",
        "weight": 0.5,
    },
    "RESCINDS": {
        "canonical": ["rescind", "cancel", "revoke", "withdraw"],
        "synonyms": ["reverse", "abandon", "drop", "scrap"],
        # Rescinding sanctions = de-escalation; rescinding a peace deal =
        # escalation. The object determines direction — leave to downstream.
        "direction": "context-dependent",
        "weight": 0.7,
    },
    "IMPOSES": {
        "canonical": ["impose", "enforce", "apply", "introduce"],
        "synonyms": [
            "levy", "implement",
            # Added 2026-04-29 from real headline data:
            "sanction", # verb form: "US sanctions firm"
            "freeze",   # 24 occurrences ("freezes assets")
            # Added 2026-04-29 (round 2) — coercive state action verbs:
            "seize",        # 48 — "Navy seizes ship", "seize assets"
            "intercept",    # 10 — "intercepts vessel"
            "arrest",       # not in top 40 but clearly coercive
            "detain",       # not in top 40 but clearly coercive
            # Removed 2026-04-29 per "primary-school-kid principle":
            # "expand" — fine for "expand sanctions" but ambiguous for
            #            "expand business", "expand stablecoin", etc.
        ],
        # Verb lemmas with common noun forms — only match when POS-confirmed:
        "ambiguous_lemmas": [
            "target",   # 43 — "targeted by sanctions" (verb) vs
                        # "$100B target", "price target", "the target" (noun)
            "place",    # "place sanctions on" (verb) vs "first place",
                        # "in place", "the place" (noun)
        ],
        "direction": "escalation",
        "weight": 0.9,
    },
    "LIFTS": {
        "canonical": ["lift", "remove", "end", "terminate"],
        "synonyms": ["dismiss", "eliminate", "abolish"],
        "direction": "de-escalation",
        "weight": 0.9,
    },
    "DEMANDS": {
        "canonical": ["demand", "require", "insist", "press"],
        "synonyms": [
            "pressure",
            # Added 2026-04-29 from real headline data:
            # "call" maps here when used as "calls for X" — the most
            # common headline pattern ("calls for resignation",
            # "calls for sanctions"). Bare "call" (phone) is rarer in
            # the news contexts we track.
            "call",     # 25 occurrences
            # Added 2026-04-29 (round 2):
            "urge",     # 16 — "urges Hormuz blockade lift"
        ],
        # "push for" is phrasal — not stored here, handled by the extractor
        # if/when we add phrasal-verb support.
        "direction": "escalation",
        "weight": 0.7,
    },
    "AGREES": {
        "canonical": ["agree", "accept", "endorse", "ratify"],
        "synonyms": [
            "consent",
            # Removed 2026-04-29 per "primary-school-kid principle":
            # "boost" — fine for "boost cooperation" but ambiguous for
            #           "boost stocks", "boost sales", "boost ratings"
        ],
        # Verb lemmas with common noun forms — only match when POS-confirmed:
        "ambiguous_lemmas": [
            "sign",     # "sign treaty" (verb) vs "warning sign", "the sign" (noun)
            "draft",    # "draft MOU" (verb) vs "the draft", "first draft" (noun)
            "back",     # "back the deal" (verb) vs "back of the report",
                        # "behind their back" (noun/adverb)
        ],
        # NB: "approve" is in ANNOUNCES (announce-approval). We keep AGREES
        # for bilateral/multilateral consent specifically.
        "direction": "de-escalation",
        "weight": 0.8,
    },
    "REJECTS": {
        "canonical": ["reject", "refuse", "deny", "decline"],
        "synonyms": [
            "rebuff", "spurn",
            # Added 2026-04-29 from real headline data:
            "fail",     # 24 occurrences ("Country fails to overturn") —
                        # failure of an action, mapping to REJECTS via
                        # negation-of-success semantics.
            # Added 2026-04-29 (round 2):
            "condemn",  # 14 — verbal opposition / hostile rejection
            # Removed 2026-04-29 per "primary-school-kid principle":
            # "stall" — fine for "talks stall" but also "engine stalls",
            #           "growth stalls"; not unambiguous enough
        ],
        # NB: "dismiss" is in LIFTS (dismiss charges). REJECTS is for refusal
        # of an offer/proposal/claim.
        "direction": "escalation",
        "weight": 0.8,
    },
    "MEETS": {
        "canonical": ["meet", "discuss", "negotiate", "talk"],
        "synonyms": ["confer", "consult"],
        # Talks happening at all is mildly de-escalatory vs the alternative
        # of no contact.
        "direction": "de-escalation",
        "weight": 0.5,
    },
}

# When direction is flipped by negation, this is the mapping. "context-
# dependent" stays context-dependent because negating an ambiguous thing
# doesn't disambiguate it.
_DIRECTION_FLIP: dict[str, str] = {
    "escalation": "de-escalation",
    "de-escalation": "escalation",
    "neutral": "neutral",
    "context-dependent": "context-dependent",
}

UNKNOWN_CATEGORY = "unknown"
UNKNOWN_DIRECTION = "unknown"


# ---------------------------------------------------------------------------
# Reverse index — built once at import time
# ---------------------------------------------------------------------------
def _build_indices() -> tuple[dict[str, str], set[str]]:
    """Build two indices over VERB_CATEGORIES:
      - lemma -> category name (covers canonical, synonyms, and
        ambiguous_lemmas all together)
      - set of lemmas marked as ambiguous (need POS-confirmation to match;
        skipped in the lemma fallback)

    If the same lemma appears under multiple categories (which we try to
    avoid), the first one wins and we warn at import. The intent is for
    the taxonomy to be a strict partition over the lemmas it covers.
    """
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for category, spec in VERB_CATEGORIES.items():
        canonical = spec.get("canonical", [])
        synonyms = spec.get("synonyms", [])
        ambiguous_lemmas = spec.get("ambiguous_lemmas", [])
        for lemma in canonical + synonyms + ambiguous_lemmas:
            lemma = lemma.lower()
            if lemma in index and index[lemma] != category:
                # Soft warning rather than hard fail — surface during dev.
                # If this fires in production, taxonomy needs cleanup.
                import warnings
                warnings.warn(
                    f"verb_taxonomy: lemma '{lemma}' is in both "
                    f"'{index[lemma]}' and '{category}'. Keeping "
                    f"'{index[lemma]}'.",
                    stacklevel=2,
                )
                continue
            index[lemma] = category
        for lemma in ambiguous_lemmas:
            ambiguous.add(lemma.lower())
    return index, ambiguous


_LEMMA_TO_CATEGORY, _AMBIGUOUS_LEMMAS = _build_indices()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def classify_verb(verb_lemma: Optional[str]) -> str:
    """Return the category name for a verb lemma, or UNKNOWN_CATEGORY.

    Case-insensitive. Returns UNKNOWN_CATEGORY for None, empty string, or
    any lemma not in the taxonomy. Ambiguous lemmas (e.g. 'target',
    'sign', 'report') still return their category here — call
    is_ambiguous_lemma() to check whether the caller should require
    POS-confirmation before trusting the category.
    """
    if not verb_lemma:
        return UNKNOWN_CATEGORY
    return _LEMMA_TO_CATEGORY.get(verb_lemma.lower(), UNKNOWN_CATEGORY)


def is_ambiguous_lemma(verb_lemma: Optional[str]) -> bool:
    """True if `verb_lemma` is marked as having a common noun form
    (the verb is in the ambiguous_lemmas slot of its category).

    The extractor uses this to decide whether to trust a lemma match
    in the POS-mistag fallback path. Ambiguous lemmas are only matched
    when the parser confirms POS=VERB or AUX.
    """
    if not verb_lemma:
        return False
    return verb_lemma.lower() in _AMBIGUOUS_LEMMAS


def direction_for(category: str, negated: bool = False) -> str:
    """Return the directional label for a category, accounting for negation.

    Unknown categories return UNKNOWN_DIRECTION regardless of negation —
    we don't speculate about direction when we don't know the action.
    """
    if category == UNKNOWN_CATEGORY:
        return UNKNOWN_DIRECTION
    spec = VERB_CATEGORIES.get(category)
    if spec is None:
        return UNKNOWN_DIRECTION
    base = spec["direction"]
    if negated:
        return _DIRECTION_FLIP.get(base, base)
    return base


def all_categories() -> list[str]:
    """Return the list of category names. Useful for tests and validation."""
    return list(VERB_CATEGORIES.keys())


def known_lemmas() -> set[str]:
    """Return the set of all lemmas the taxonomy recognises (including
    ambiguous lemmas)."""
    return set(_LEMMA_TO_CATEGORY.keys())


def ambiguous_lemmas() -> set[str]:
    """Return the set of lemmas that are POS-confirmation-required
    (skipped in the lemma fallback path)."""
    return set(_AMBIGUOUS_LEMMAS)
