"""Debunk-rating identification for PesaCheck ingest.

Every PesaCheck fact-check leads its headline with the verdict, in the article's
own language and set off by a colon: ``ALTERED: ...``, ``FAUX : ...``,
``KHIYAANO: ...``, ``PARTLY FALSE: ...``. Superdesk records that verdict as a
single-select custom vocabulary — the ``Debunk`` scheme — so the prefix has to be
mapped onto one of its qcodes and attached to the item as a subject entry. That
is the same mechanism the tag-derived fields use; see ``pesacheck/tags.py``.

**The qcodes are defined by the tracked content config, not by this map.** The
``Debunk`` vocabulary under ``server/data/vocabularies/editorial/Debunk.json`` is
what ``app:initialize_data`` seeds and therefore what a newsroom can actually
resolve; this map can be orphaned by an edit to that file. It had been orphaned
for a while: three of the ten ratings below (``true``, ``misleading``,
``mixture``) existed only here until they were added to the vocabulary
(2026-08-28), which is what ``tests/test_tags.py`` now guards.

The map is multilingual because the prefix is written in whichever of the six
languages the article is in. Where a language has no exact counterpart to a
rating the closest one stands in — English ``FAKE`` -> ``Hoax``, since the posts
it prefixes are scams. A prefix that matches nothing here leaves the rating
unset rather than guessing; an editor can still set it by hand.
"""

import re

# The subject ``scheme`` that marks an entry as a Debunk rating, and the qcode ->
# display-name map for that vocabulary. Both the name and the qcode are stored on
# the entry so the client renders the label without a vocabulary lookup -- which
# is also why a stale name here shows the wrong text rather than failing.
# Source of truth for both: the tracked "Debunk" vocabulary at
# data/vocabularies/editorial/Debunk.json. tests/test_tags.py asserts every
# qcode and name below still matches it.
DEBUNK_SCHEME = "Debunk"

_RATING_NAMES = {
    "altered": "Altered",
    "false": "False",
    "falsehead": "False headline",
    "hoax": "Hoax",
    "context": "Missing context",
    "partfalse": "Partly false",
    "satire": "Satire",
    "misleading": "Misleading",
    "true": "True",
    "mixture": "Mixture",
}

# Verdict prefix -> Debunk qcode. Keys are matched after upper-casing and
# collapsing internal whitespace (see ``_normalise_prefix``); the Ethiopic keys
# are caseless and pass through that unchanged. The Latin-script languages
# (English, French, Somali, Afaan Oromo, Kiswahili) all upper-case cleanly.
_RATING_BY_PREFIX = {
    # English
    "ALTERED": "altered",
    "DOCTORED": "altered",
    "FALSE": "false",
    "FALSE HEADLINE": "falsehead",
    "HOAX": "hoax",
    "FAKE": "hoax",
    "MISSING CONTEXT": "context",
    "PARTLY FALSE": "partfalse",
    "PARTY FALSE": "partfalse",  # common typo of "PARTLY FALSE"
    "SATIRE": "satire",
    "SATIRICAL": "satire",
    "MISLEADING": "misleading",
    "SCAM": "false",  # scams are a kind of false claim; no separate qcode
    "TRUE": "true",
    "MIXTURE": "mixture",
    # French
    "MANIPULÉE": "altered",
    "MANIPULÉ": "altered",
    "MANIPULÉES": "altered",
    "MANIPULE": "altered",  # unaccented
    "TRUQUÉE": "altered",
    "TRUQUÉ": "altered",
    "TRUQUE": "altered",
    "MODIFIÉE": "altered",
    "RETOUCHÉE": "altered",
    "RETOUCHÉ": "altered",
    "FAUX": "false",
    "FAUSSE": "false",
    "INTOX": "false",
    "FAUX TITRE": "falsehead",
    "CANULAR": "hoax",
    "CONTEXTE MANQUANT": "context",
    "CONTEXT MANQUANT": "context",  # common typo
    "HORS CONTEXTE": "context",
    "PARTIELLEMENT FAUX": "partfalse",
    "PATIELLEMENT FAUX": "partfalse",  # common typo
    "SATIRIQUE": "satire",
    "TROMPEUR": "misleading",
    "VRAI": "true",
    # Somali
    "WAX LAGA BEDALAY": "altered",
    "BEEN": "false",
    "BEEN AH": "false",
    "QEYB AHAAN BEEN AH": "partfalse",
    "BEEN ABUUR": "hoax",
    "KHIYAANO": "hoax",
    "MAADEYSI": "satire",
    "HAREERMARSAN XAQIIQDA": "misleading",
    "HAREERMARSAN XAQIIQADA": "misleading",
    "XAALADDA HAREERMARSAN": "misleading",
    # Kiswahili
    "SI KWELI": "false",
    "UONGO": "false",
    "UWONGO": "false",
    "IMEBADILISHWA": "altered",
    "IMEBALISHWA": "altered",  # common typo
    "KICHWA CHA HABARI POTOFU": "falsehead",
    "UONGO KWA SEHEMU": "partfalse",
    "GHUSHI": "hoax",
    "FEKI": "hoax",
    "UZUSHI": "hoax",
    # Amharic
    "የተቀየረ": "altered",
    "ሐሰት": "false",
    "ሐስት": "false",  # spelling variant
    "ሐሰተኛ": "false",
    "ውሸት": "false",  # synonym of ሐሰት
    "ሐሰተኛ አርዕስት": "falsehead",
    "በከፊል ሐሰት": "partfalse",
    "በከፊል ሀሰት": "partfalse",  # spelling variant
    "በከፊል ሐሰተኛ": "partfalse",
    "በከፊል ውሸት": "partfalse",  # synonym of በከፊል ሐሰት
    "ከአውድ ውጪ": "context",
    "ከዓውድ ውጪ": "context",  # spelling variant
    "ከአውድ ውጭ": "context",  # spelling variant
    "ስላቅ": "satire",
    "የፈጠራ ወሬ": "hoax",
    "የተጭበረበረ": "hoax",
    # Afaan Oromo
    "SOBA": "false",
    "DHUGAA MITTI": "false",
    "DHUGAA MITII": "false",
    "GAR-TOKKOON SOBA": "partfalse",
    "GAR TOKKOON SOBA": "partfalse",
    "GAR-TOKKON SOBA": "partfalse",  # common typo
    "HAALAAN ALA": "context",
    "KAN JIJJIIRAME": "altered",
    "AFAANFAAJJESSA": "misleading",
}

# A headline's verdict is the text before the first colon. Accept the ASCII colon,
# the fullwidth colon (turns up in exports), and the Ethiopic wordspace ``፡`` /
# preface colon ``፦`` / full stop ``።`` / semicolon ``፤`` / comma ``፣`` — Amharic
# headlines delimit the verdict with any of these rather than ``:``, so without
# them Amharic verdicts read as "no prefix". Cap the length so a stray colon deep
# in a sentence can't be read as an enormous "prefix".
_PREFIX_RE = re.compile(r"^\s*(.{1,40}?)\s*[:：፡፦።፤፣]")


def _normalise_prefix(prefix):
    return " ".join(prefix.split()).upper()


def debunk_rating(title):
    """Return the ``Debunk`` subject entry for a headline, or ``None``.

    ``None`` means the headline has no colon-delimited prefix, or the prefix is
    not a verdict this map knows — either way the item is left without a rating.
    """
    if not title:
        return None
    match = _PREFIX_RE.match(title)
    if not match:
        return None
    qcode = _RATING_BY_PREFIX.get(_normalise_prefix(match.group(1)))
    if not qcode:
        return None
    return {"name": _RATING_NAMES[qcode], "qcode": qcode, "scheme": DEBUNK_SCHEME}
