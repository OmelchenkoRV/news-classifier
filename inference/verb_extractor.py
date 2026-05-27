"""
VerbExtractor — spaCy-based extraction of subject, verb, object, and
negation from headlines, with directional taxonomy classification.

Stage 2 of the classification cascade. Sits between regex (Stage 1) and
NLI (Stage 3). For each headline, returns a structured dict that the
trigger system uses to disambiguate same-entity headlines that have
opposite directional implications.

Typical usage:

    extractor = VerbExtractor()  # loads en_core_web_sm, ~50MB, one-time
    result = extractor.extract("Iran threatens to close Strait of Hormuz")
    # {
    #     "subject": "iran",
    #     "verb": "threaten",
    #     "verb_category": "ATTACKS",
    #     "object": "hormuz",
    #     "negated": False,
    #     "direction": "escalation",
    # }

The extractor is stateless after init and thread-safe for read access
to the loaded pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional, TYPE_CHECKING

from config.verb_taxonomy import (
    classify_verb,
    direction_for,
    UNKNOWN_CATEGORY,
    UNKNOWN_DIRECTION,
)

if TYPE_CHECKING:
    from spacy.tokens import Doc, Token

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "en_core_web_sm"


@dataclass
class ExtractionResult:
    """Structured result from VerbExtractor.extract().

    Fields are all lowercase strings or None / bool. The dict form (via
    to_dict / asdict) is what gets serialised into the trigger system
    and alerter payloads.
    """
    subject: Optional[str]
    verb: Optional[str]              # lemma, lowercase
    verb_category: str               # category name or UNKNOWN_CATEGORY
    object: Optional[str]
    negated: bool
    direction: str                   # 'escalation' / 'de-escalation' / 'neutral' / 'context-dependent' / 'unknown'

    def to_dict(self) -> dict:
        return asdict(self)


# Tokens used as auxiliary negators that don't show up as the spaCy `neg`
# dependency. These are checked as a fallback when the parser doesn't tag
# negation directly — common in headline-style English where "rules out"
# or "denies" carry the negation lexically rather than syntactically.
_LEXICAL_NEGATORS = {
    "rule",       # "rules out X"
    "deny",       # "denies plans to X"
    "refuse",     # "refuses to X"
    "decline",    # "declines to X"
    "reject",     # "rejects X" — but REJECTS is itself a category;
                  # see _is_negated_action for resolution.
}

# Particles that turn certain verbs into negators. "rule out" = negator;
# "rule" alone is not.
_NEGATING_PARTICLES = {
    "rule": {"out"},
}


class VerbExtractor:
    """Wraps a spaCy pipeline to extract directional structure from headlines."""

    def __init__(self, model: str = DEFAULT_MODEL, nlp=None):
        """Load the spaCy model. Pass `nlp` to inject a pre-loaded pipeline
        (useful for tests and for sharing one model across services).
        """
        if nlp is not None:
            self.nlp = nlp
        else:
            try:
                import spacy
            except ImportError as e:
                raise ImportError(
                    "spaCy is required for VerbExtractor. "
                    "Install with: pip install spacy"
                ) from e
            try:
                self.nlp = spacy.load(model)
            except OSError as e:
                raise OSError(
                    f"spaCy model '{model}' not found. "
                    f"Install with: python -m spacy download {model}"
                ) from e

    # ---------------------------------------------------------------- public

    def extract(self, text: str) -> ExtractionResult:
        """Parse `text` and return an ExtractionResult.

        For empty/whitespace input, returns an all-None result with
        unknown category and direction. Never raises on parse errors —
        the cascade depends on this stage being non-fatal.
        """
        if not text or not text.strip():
            return ExtractionResult(
                subject=None,
                verb=None,
                verb_category=UNKNOWN_CATEGORY,
                object=None,
                negated=False,
                direction=UNKNOWN_DIRECTION,
            )

        try:
            doc = self.nlp(text)
        except Exception as e:
            # spaCy failures shouldn't take down the pipeline. Log and
            # return an unknown result; Stage 3 NLI will pick up the slack.
            logger.warning("spaCy parse failed for text=%r: %s", text, e)
            return ExtractionResult(
                subject=None,
                verb=None,
                verb_category=UNKNOWN_CATEGORY,
                object=None,
                negated=False,
                direction=UNKNOWN_DIRECTION,
            )

        root_verb = self._find_root_verb(doc)
        if root_verb is None:
            return ExtractionResult(
                subject=None,
                verb=None,
                verb_category=UNKNOWN_CATEGORY,
                object=None,
                negated=False,
                direction=UNKNOWN_DIRECTION,
            )

        # If the root is a "soft" reporting verb like "threaten" / "plan" /
        # "offer" / "propose", the action of interest is its complement
        # (xcomp / ccomp). E.g. "Iran threatens to close Hormuz": the
        # verb that bears directional weight is "close", not "threaten".
        # We try the complement first; if it's classifiable, we use it.
        # If not, we fall back to the root.
        action_verb, action_negated_extra = self._resolve_action_verb(root_verb)

        verb_lemma = action_verb.lemma_.lower()
        category = classify_verb(verb_lemma)

        # If the complement turned out to be unclassified, retry with the
        # root verb itself — for headlines like "Iran threatens Israel"
        # there's no complement and "threaten" is the directional verb.
        if category == UNKNOWN_CATEGORY and action_verb is not root_verb:
            verb_lemma = root_verb.lemma_.lower()
            category = classify_verb(verb_lemma)
            action_verb = root_verb
            action_negated_extra = False

        negated = self._is_negated(root_verb) or self._is_negated(action_verb) or action_negated_extra

        subject = self._find_subject(root_verb)
        # Object is taken from the action verb (which may differ from root
        # for "threatens to close X" — we want X, not the complement clause).
        obj = self._find_object(action_verb)

        return ExtractionResult(
            subject=_normalise(subject),
            verb=verb_lemma,
            verb_category=category,
            object=_normalise(obj),
            negated=negated,
            direction=direction_for(category, negated),
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _find_root_verb(doc: "Doc") -> Optional["Token"]:
        """Return the syntactic root if it is a verb. spaCy's parser usually
        sets the main predicate as the root of the first sentence.

        Falls back through three layers, increasing in tolerance for
        parse errors:
          1. Sentence root, if its POS is VERB or AUX.
          2. Any token in the sentence with POS=VERB.
          3. Lemma-fallback against the taxonomy. This catches POS-mistag
             cases like "EU lifts trade restrictions" (where 'lifts' is
             tagged NOUN) and "Putin and Trump meet" (where 'meet' is
             tagged NOUN).

        Two safeguards on the lemma fallback:
          - PROPN tokens sandwiched between other PROPNs are skipped to
            avoid matching company / surname runs ("Apple Lift Tesla").
          - Lemmas marked as `ambiguous_lemmas` in the taxonomy
            (target, sign, report, etc. — verbs that are also common
            nouns) are skipped, because the noun reading is more likely
            in headlines like "XRP eyes $100B target". The taxonomy
            still classifies these correctly when the parser confirms
            POS=VERB/AUX in layer 1 or 2 — only the fallback is
            constrained.

        Subject / object extraction in fallback cases may still come out
        wrong since the dependency parse rests on broken POS tags — but
        verb_category and direction (what trigger routing depends on)
        are correct.
        """
        # Lazy import to avoid pulling the taxonomy at module load if
        # something else imports verb_extractor purely for type hints.
        from config.verb_taxonomy import known_lemmas, is_ambiguous_lemma
        taxonomy_lemmas = known_lemmas()

        for sent in doc.sents:
            # Layer 1: root is properly tagged as a verb.
            root = sent.root
            if root.pos_ in ("VERB", "AUX"):
                return root
            # Layer 2: any token tagged VERB.
            for tok in sent:
                if tok.pos_ == "VERB":
                    return tok
            # Layer 3: lemma-match against the taxonomy. Skip:
            #   - ambiguous lemmas (require POS-confirmation)
            #   - PROPN tokens sandwiched between PROPNs (name runs)
            tokens = list(sent)
            for i, tok in enumerate(tokens):
                lemma = tok.lemma_.lower()
                if lemma not in taxonomy_lemmas:
                    continue
                if is_ambiguous_lemma(lemma):
                    continue
                if tok.pos_ == "PROPN":
                    prev_propn = i > 0 and tokens[i - 1].pos_ == "PROPN"
                    next_propn = (
                        i + 1 < len(tokens) and tokens[i + 1].pos_ == "PROPN"
                    )
                    if i != 0 and (prev_propn or next_propn):
                        continue
                return tok
        return None

    def _resolve_action_verb(self, root: "Token") -> tuple["Token", bool]:
        """If root is a reporting/modal verb whose complement carries the
        real action, return that complement. Otherwise return root.

        Returns (action_verb, extra_negated). `extra_negated` is True when
        the resolution path itself implies negation — e.g. "rules out
        closing" — so the caller can flip direction even if the action
        verb has no `neg` dependency of its own.
        """
        # Lexical negators that swallow their complement: "ruled out
        # closing Hormuz" — the action is "close" but it's negated by
        # "rule out".
        if self._is_lexical_negator(root):
            comp = self._find_complement_verb(root)
            if comp is not None:
                return comp, True
            # No complement found — root itself is the action, and it's
            # implicitly negated. Rare in practice ("Iran rules out").
            return root, True

        # Reporting/modal verbs: prefer their complement as the action.
        # "threaten", "plan", "offer", "propose", "agree" — any verb that
        # commonly takes a verb complement and where the complement is
        # what trades on direction.
        comp = self._find_complement_verb(root)
        if comp is not None:
            return comp, False

        return root, False

    @staticmethod
    def _find_complement_verb(verb: "Token") -> Optional["Token"]:
        """Return the xcomp/ccomp verb child of `verb`, if any."""
        for child in verb.children:
            if child.dep_ in ("xcomp", "ccomp") and child.pos_ in ("VERB", "AUX"):
                return child
        return None

    def _is_lexical_negator(self, verb: "Token") -> bool:
        """Check if `verb` is a lexical negator (e.g. 'rule out', 'deny').

        For phrasal cases (rule + out) we require the particle to be
        present. Bare 'deny', 'refuse', 'decline' count on their own.
        """
        lemma = verb.lemma_.lower()
        if lemma not in _LEXICAL_NEGATORS:
            return False
        required_particles = _NEGATING_PARTICLES.get(lemma)
        if required_particles is None:
            # Bare lemma is sufficient (e.g. "denies", "refuses").
            # NB: "reject" is in _LEXICAL_NEGATORS but is also a category
            # in the taxonomy. We do NOT treat it as a lexical negator
            # of its own complement because "rejects deal" should map to
            # REJECTS, not "AGREES negated". So we exclude it here:
            if lemma == "reject":
                return False
            return True
        # Phrasal: check for the particle as a `prt` dependent.
        for child in verb.children:
            if child.dep_ == "prt" and child.lower_ in required_particles:
                return True
        return False

    @staticmethod
    def _is_negated(verb: "Token") -> bool:
        """True if the verb has a direct `neg` dependency child."""
        for child in verb.children:
            if child.dep_ == "neg":
                return True
        return False

    @staticmethod
    def _find_subject(verb: "Token") -> Optional[str]:
        """Return the text of the nominal subject of `verb`, or None.

        Handles `nsubj` and `nsubjpass`. Returns the full subtree text so
        compound subjects ("US Treasury Secretary") come through whole.
        """
        for child in verb.children:
            if child.dep_ in ("nsubj", "nsubjpass"):
                return _subtree_text(child)
        return None

    @staticmethod
    def _find_object(verb: "Token") -> Optional[str]:
        """Return the text of the direct or prepositional object, or None.

        Priority: dobj > pobj of the first prep child > attr. Returns the
        full subtree so multi-word objects ("Strait of Hormuz") come
        through whole.
        """
        # Direct object first.
        for child in verb.children:
            if child.dep_ == "dobj":
                return _subtree_text(child)
        # Then prepositional object — common with verbs like "agree to",
        # "negotiate with", "fire on".
        for child in verb.children:
            if child.dep_ == "prep":
                for grandchild in child.children:
                    if grandchild.dep_ == "pobj":
                        return _subtree_text(grandchild)
        # Then attribute/copular complement.
        for child in verb.children:
            if child.dep_ == "attr":
                return _subtree_text(child)
        return None


# --------------------------------------------------------------- module utils

def _subtree_text(token: "Token") -> str:
    """Return the surface text of a token's full subtree, joined with spaces.
    Strips punctuation tokens at the boundaries."""
    tokens = [t for t in token.subtree if not t.is_punct]
    return " ".join(t.text for t in tokens)


def _normalise(s: Optional[str]) -> Optional[str]:
    """Lowercase and strip; return None for empty results."""
    if s is None:
        return None
    s = s.strip().lower()
    return s or None
