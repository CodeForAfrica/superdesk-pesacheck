"""Language identification for PesaCheck ingest.

PesaCheck publishes in six languages, and Superdesk needs an ISO 639-1 code on
every ingested item. Two strategies are used, in order:

1. :func:`detect_language_from_tags` — recent posts carry an explicit language
   tag (``English``, ``Kiswahili``, ``Afaan Oromo``, ...). This is editorial
   metadata, so it is authoritative and is always tried first.
2. :func:`detect_language_from_text` — older posts predate the tagging
   convention, so the body text is classified directly.

The text classifier is deliberately closed over just these six languages. A
general-purpose detector is a poor fit here: ``langdetect`` has no model for
Afaan Oromo at all and mislabels it as Somali (both are Cushitic and share the
doubled-vowel Latin orthography), and it raises on Amharic. Scoring only the
languages we actually publish in avoids losing articles to a seventh language
that was never an option.
"""

import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import TypeAlias

from .language_markers import (
    AMHARIC,
    ENGLISH,
    FRENCH,
    KISWAHILI,
    MARKERS,
    OROMO,
    SOMALI,
    LanguageCode,
)

logger = logging.getLogger(__name__)


# A ``(sort_order, label)`` pair as Ghost exports it, where ``label`` is a tag
# display name or slug. Both come straight from JSON, so both may be null and
# neither is guaranteed to be a string.
TagLabel: TypeAlias = tuple[int | None, object]


# The languages PesaCheck publishes in, as ISO 639-1 codes.
SUPPORTED_LANGUAGES: tuple[LanguageCode, ...] = (
    ENGLISH,
    KISWAHILI,
    FRENCH,
    SOMALI,
    OROMO,
    AMHARIC,
)

# Used when detection is impossible (no tags, empty body). English is the
# house language, so it is the least surprising thing to fall back to.
DEFAULT_LANGUAGE: LanguageCode = ENGLISH


# Ghost tag names/slugs that identify a language, normalised by
# ``_normalise_tag``. Both the English name and the endonym are accepted, along
# with ISO 639-1/639-2 codes in case a tag is ever created from a code.
#
# Matching is exact on the whole normalised string so that language tags are
# never confused with the country tags they sit next to: PesaCheck tags posts
# with both ``French``/``France`` and ``Somali``/``Somalia``.
#
# Deliberately absent: bare ``Oromo``. Unlike ``Amharic`` or ``Kiswahili`` it is
# an ethnonym before it is a language name, and it is a plausible topic tag on
# an English article about Oromo people. PesaCheck's convention is the
# unambiguous ``Afaan Oromo``, and Oromo prose is recognised well by the text
# classifier, so an unmatched tag degrades safely.
_LANGUAGE_TAGS: dict[str, LanguageCode] = {
    "english": ENGLISH,
    "en": ENGLISH,
    "eng": ENGLISH,
    "kiswahili": KISWAHILI,
    "swahili": KISWAHILI,
    "sw": KISWAHILI,
    "swa": KISWAHILI,
    "french": FRENCH,
    "francais": FRENCH,
    "français": FRENCH,
    "fr": FRENCH,
    "fra": FRENCH,
    "somali": SOMALI,
    "soomaali": SOMALI,
    "af soomaali": SOMALI,
    "so": SOMALI,
    "som": SOMALI,
    "afaan oromo": OROMO,
    "afaan oromoo": OROMO,
    "afan oromo": OROMO,
    "oromiffa": OROMO,
    "oromifa": OROMO,
    "oromoo": OROMO,
    "om": OROMO,
    "orm": OROMO,
    "amharic": AMHARIC,
    "amarigna": AMHARIC,
    "amharigna": AMHARIC,
    "አማርኛ": AMHARIC,
    "am": AMHARIC,
    "amh": AMHARIC,
}

# Runs of Ethiopic characters, covering the block plus its supplement and
# extensions (U+1200-U+139F merges Ethiopic and Ethiopic Supplement). Amharic is
# the only Ethiopic-script language PesaCheck publishes in.
#
# Both of these count characters by matching *runs* and summing their lengths,
# which keeps the scan in C: a per-character Python loop over a multi-kilobyte
# body cost ~1.2ms per post, around 90% of the whole text-detection path.
_ETHIOPIC_RUN_RE: re.Pattern[str] = re.compile(
    "["
    "ሀ-᎟"  # Ethiopic + Ethiopic Supplement
    "ⶀ-⷟"  # Ethiopic Extended
    "꬀-꬯"  # Ethiopic Extended-A
    "\U0001e7e0-\U0001e7ff"  # Ethiopic Extended-B
    "]+"
)

# Approximates ``str.isalpha`` per character: word characters excluding digits
# and underscore.
_ALPHABETIC_RUN_RE: re.Pattern[str] = re.compile(r"[^\W\d_]+", re.UNICODE)

# Share of cased letters that must be Ethiopic before a post is called Amharic.
# Amharic posts in the reference export are >99% Ethiopic, and Latin-script
# posts contain none, so there is a wide margin here. A *share* rather than
# "contains any Ethiopic character" matters: English and Afaan Oromo articles
# about Ethiopia routinely quote an Amharic phrase, and one quote must not
# relabel the whole article.
_ETHIOPIC_SHARE_THRESHOLD: float = 0.15

# Coverage a native document is expected to reach for each language, used to
# put the raw scores on a comparable scale. Without this the argmax just
# favours whichever marker list happens to cover the most running text.
_EXPECTED_COVERAGE: dict[LanguageCode, float] = {
    ENGLISH: 0.35,
    FRENCH: 0.36,
    KISWAHILI: 0.28,
    SOMALI: 0.26,
    OROMO: 0.25,
}

# Tokens are lowercased runs of letters, keeping the apostrophes that Afaan
# Oromo uses for the glottal stop ("ta'e") so those stay single tokens.
_TOKEN_RE: re.Pattern[str] = re.compile(r"[^\W\d_]+(?:['’ʼ][^\W\d_]+)*", re.UNICODE)

# Below this many tokens the marker counts are too sparse to separate the
# candidates, so text detection declines rather than guessing.
_MIN_TOKENS: int = 8

# The winner must beat the runner-up by this ratio. Fact checks quote the
# claim they are debunking, so a Somali or Oromo article always contains some
# English; requiring a margin keeps those quotes from tipping the result.
_MIN_MARGIN: float = 1.15


def _normalise_tag(value: object) -> str:
    """Casefold a tag and collapse punctuation/whitespace to single spaces."""
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value)).casefold()
    return re.sub(r"[\s\-_]+", " ", text).strip()


def normalise_language_code(value: object) -> LanguageCode | None:
    """Normalise a Ghost ``locale`` to a language code, or return ``None``.

    Accepts anything BCP 47-ish (``en``, ``en-US``, ``am_ET``) and keeps only
    the base subtag. Codes outside :data:`SUPPORTED_LANGUAGES` are passed
    through rather than dropped, so a locale PesaCheck starts publishing in
    later still reaches Superdesk.
    """
    if not value:
        return None
    base = _normalise_tag(re.split(r"[-_]", str(value).strip())[0])
    if not base:
        return None
    return _LANGUAGE_TAGS.get(base, base)


def detect_language_from_tags(tags: Iterable[TagLabel]) -> LanguageCode | None:
    """Return the language code named by a post's tags, or ``None``.

    ``tags`` is an iterable of ``(sort_order, name)`` pairs; Ghost exposes both
    a display name and a slug per tag and either may be passed. When a post
    carries more than one language tag the lowest ``sort_order`` wins, matching
    Ghost's notion of a primary tag — in practice PesaCheck puts the language
    first, so this is also what an editor would expect.
    """
    matches: list[tuple[int, LanguageCode]] = []
    for sort_order, name in tags:
        code = _LANGUAGE_TAGS.get(_normalise_tag(name))
        if code:
            matches.append((sort_order if sort_order is not None else 0, code))

    if not matches:
        return None
    return min(matches, key=lambda pair: pair[0])[1]


def _count_matched_chars(pattern: re.Pattern[str], text: str) -> int:
    return sum(len(run) for run in pattern.findall(text))


def _ethiopic_share(text: str) -> float:
    """Fraction of the alphabetic characters in ``text`` that are Ethiopic."""
    alphabetic = _count_matched_chars(_ALPHABETIC_RUN_RE, text)
    if not alphabetic:
        return 0.0
    return _count_matched_chars(_ETHIOPIC_RUN_RE, text) / alphabetic


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


def _score_markers(tokens: Sequence[str]) -> dict[LanguageCode, float]:
    """Return ``{language: calibrated coverage}`` for the Latin-script set."""
    total = len(tokens)
    scores: dict[LanguageCode, float] = {}
    for language, markers in MARKERS.items():
        hits = sum(1 for token in tokens if token in markers)
        scores[language] = (hits / total) / _EXPECTED_COVERAGE[language]
    return scores


def detect_language_from_text(text: str | None) -> LanguageCode | None:
    """Classify ``text`` as one of :data:`SUPPORTED_LANGUAGES`, or ``None``.

    ``None`` means "not enough evidence" — too little text, or no candidate
    clearly ahead — and lets the caller decide what to do rather than having a
    coin-flip recorded as a real language.
    """
    if not text or not text.strip():
        return None

    if _ethiopic_share(text) >= _ETHIOPIC_SHARE_THRESHOLD:
        return AMHARIC

    tokens = _tokenize(text)
    if len(tokens) < _MIN_TOKENS:
        return None

    scores = _score_markers(tokens)
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    (best, best_score), (_, runner_up_score) = ranked[0], ranked[1]

    if best_score <= 0:
        return None
    if runner_up_score > 0 and best_score / runner_up_score < _MIN_MARGIN:
        logger.debug("Language detection inconclusive, scores=%s", ranked[:3])
        return None
    return best


def detect_language(
    tags: Iterable[TagLabel],
    text: str | None = "",
    default: LanguageCode | None = DEFAULT_LANGUAGE,
) -> LanguageCode | None:
    """Best-effort language for a post: tags first, then text, then ``default``."""
    return detect_language_from_tags(tags) or detect_language_from_text(text) or default
