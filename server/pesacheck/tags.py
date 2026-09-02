"""Ghost tag -> Superdesk custom-vocabulary mapping for PesaCheck ingest.

The Article content profile exposes eleven custom-vocabulary fields -- Primary
country, Countries mentioned, Debunk language, Content Type, Claim topic,
Primary platform and five more -- and until this module existed ingest populated
none of them. Every Ghost tag went to ``keywords``, a field that profile does
not expose at all: the tags were stored on the item in Superdesk but stripped
from the ninjs on the way out, so they never reached Publisher. Measured on the
local stack 2026-08-28: ``keywords`` was absent from all 235 transmitted
payloads and Publisher's ``swp_keyword`` table held zero rows. The Article
profile's ``keywords`` schema+editor entry now lives in the tracked content
config (``server/data/content_types.json``); AGENTS.md §4 has the mechanism.

The tags to fill most of those fields were already in the export and already
being parsed. Measured 2026-08-28 over the full 301-file pesacheck.org export
(14,686 ingestable posts, 49,985 public tag applications):

    Field                Scheme             Select   Posts populated
    Primary country      countrymention1    single   98.8%
    Countries mentioned  countries          multi    98.9%
    Debunk language      Debunklang         single   84.5%
    Content Type         content_type       single   84.1%
    Claim topic          Harm_type          multi     6.6%
    Primary platform     platform           single    3.1%
    GEC category         GEC                single    9.9%  (written, not wired)

Another 7.6% of tag applications (1,426 distinct tags) match nothing and stay in
``keywords``; 5,248 applications are Ghost's internal ``#Import`` bookkeeping and
1,281 are self-referential. 3,852 posts carry two or more countries, which is
what makes the single-select tiebreak below matter.

Snapshot, not an invariant -- re-derive with ``scripts/ghost/probe_tag_mapping.py``.

Like ``debunk.py``, every field here is a ``subject`` entry:
``{"name": ..., "qcode": ..., "scheme": <vocabulary _id>}``. So this is one
classifier and one call site rather than six integrations, and the entries this
returns sit in the same list ``debunk_rating()``'s does.

Three things worth knowing before changing any of it:

**A tag is offered to every scheme.** ``France`` is both a country and a GEC
category; subject entries are scheme-scoped, so populating both is not a
collision. Single-select schemes take the first match in Ghost ``sort_order``,
which ``_parse_language`` already treats as meaningful; multi-select schemes take
all of them.

**This is not where ``item["language"]`` comes from.** ``language.py`` reads the
same language tags and sets ``item["language"]``, which is what Publisher routes
on via ``article.getLocale()``; ``Debunklang`` is a separate editorial field that
merely happens to derive from the same tags. Conflating them breaks routing, and
an item that routes nowhere is invisible -- see
``docs/postmortems/publish-delivery.md``.

**The qcodes are not ours.** They come from vocabularies the content-config dump
owns outright and replaces wholesale on every seed, so a UAT refresh can orphan
one silently. ``tests/test_tags.py`` is what turns that into a failing test;
treat a dump refresh as a review pass over ``tag_vocabularies.py``.
"""

import re
import unicodedata

from pesacheck.tag_vocabularies import (
    CLAIM_TOPIC_ALIASES,
    CLAIM_TOPICS,
    CONTENT_TYPE_ALIASES,
    CONTENT_TYPES,
    COUNTRIES,
    COUNTRY_ALIASES,
    DEBUNK_LANGUAGE_ALIASES,
    DEBUNK_LANGUAGES,
    DROPPED_TAGS,
    GEC_ALIASES,
    GEC_CATEGORIES,
    PLATFORM_ALIASES,
    PLATFORMS,
    PRIMARY_COUNTRIES,
)

_NON_ALPHANUMERIC_RE = re.compile(r"[^a-z0-9]+")


def normalise_tag(text):
    """Fold tag text onto a comparison key: NFKD, drop marks, lower, strip punctuation.

    This is what collapses ``Côte d'Ivoire``, ``Cote Divoire`` and
    ``COTE D'IVOIRE`` onto one key. It is also what makes the double-encoded
    ``CÃ´te d'Ivoire`` in the seeded ``countries`` vocabulary harmless *to
    matching* -- though not to display, which is why
    ``bootstrap_superdesk.py`` repairs the name itself.

    Returns ``""`` for anything that is not a non-empty string, so callers can
    treat a falsy key as "no tag here".
    """
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALPHANUMERIC_RE.sub("", stripped.lower())


def _lookup(names_by_qcode, aliases):
    """Build {normalised tag: qcode} from a vocabulary table plus its aliases.

    The vocabulary's own display names are keys in their own right -- a tag that
    matches the label needs no alias -- and aliases are layered on top, so an
    alias can override a label that happens to normalise onto the wrong thing.
    """
    lookup = {}
    for qcode, name in names_by_qcode.items():
        key = normalise_tag(name)
        if key:
            lookup.setdefault(key, qcode)
    lookup.update(aliases)
    return lookup


# One scheme's worth of mapping. `multi` decides whether a second matching tag
# is a second subject entry or is ignored: `countrymention1` is single-select in
# the vocabulary, so writing two entries there produces an item the authoring
# view cannot represent.
class _Scheme:
    def __init__(self, scheme, names_by_qcode, aliases, multi):
        self.scheme = scheme
        self.names_by_qcode = names_by_qcode
        self.lookup = _lookup(names_by_qcode, aliases)
        self.multi = multi

    def qcode_for(self, key):
        return self.lookup.get(key)

    def entry(self, qcode):
        return {
            "name": self.names_by_qcode[qcode],
            "qcode": qcode,
            "scheme": self.scheme,
        }


# `countrymention1` is the same ISO 3166-1 alpha-3 qcodes as `countries`, but
# only for the 53 African countries its vocabulary lists -- so it shares the
# country alias table and filters by its own membership. A tag for a
# non-African country still populates `countries`; it just cannot be the
# primary country, which is what that field means.
PRIMARY_COUNTRY = "countrymention1"
COUNTRIES_SCHEME = "countries"
DEBUNK_LANGUAGE = "Debunklang"
CONTENT_TYPE = "content_type"
CLAIM_TOPIC = "Harm_type"
PLATFORM = "platform"
GEC = "GEC"

_SCHEMES = {
    PRIMARY_COUNTRY: _Scheme(
        PRIMARY_COUNTRY,
        PRIMARY_COUNTRIES,
        {
            key: qcode
            for key, qcode in COUNTRY_ALIASES.items()
            if qcode in PRIMARY_COUNTRIES
        },
        multi=False,
    ),
    COUNTRIES_SCHEME: _Scheme(COUNTRIES_SCHEME, COUNTRIES, COUNTRY_ALIASES, multi=True),
    DEBUNK_LANGUAGE: _Scheme(
        DEBUNK_LANGUAGE, DEBUNK_LANGUAGES, DEBUNK_LANGUAGE_ALIASES, multi=False
    ),
    CONTENT_TYPE: _Scheme(
        CONTENT_TYPE, CONTENT_TYPES, CONTENT_TYPE_ALIASES, multi=False
    ),
    CLAIM_TOPIC: _Scheme(CLAIM_TOPIC, CLAIM_TOPICS, CLAIM_TOPIC_ALIASES, multi=True),
    PLATFORM: _Scheme(PLATFORM, PLATFORMS, PLATFORM_ALIASES, multi=False),
    GEC: _Scheme(GEC, GEC_CATEGORIES, GEC_ALIASES, multi=False),
}

# The schemes ingest actually populates. `GEC` is built above and excluded here
# on purpose: at 9.8% coverage it is the weakest inference of the seven, since a
# post merely tagged `France` would be filed under the France category whether
# or not France is what the claim is about. Deferred to a second pass pending a
# look at the dry-run output (decision recorded 2026-08-28 in
# docs/plans/ghost-tag-field-mapping.md). Adding "GEC" here is the whole change.
WIRED_SCHEMES = (
    PRIMARY_COUNTRY,
    COUNTRIES_SCHEME,
    DEBUNK_LANGUAGE,
    CONTENT_TYPE,
    CLAIM_TOPIC,
    PLATFORM,
)


def is_public_tag(tag):
    """Whether a Ghost tag should reach the item at all.

    Ghost marks internal tags ``visibility: internal`` and names them with a
    leading ``#``; the export's eleven ``#Import <timestamp>`` tags account for
    5,248 applications across the corpus. Both checks are applied because the
    export is not consistent about carrying ``visibility`` -- older files omit
    the column entirely, and a missing value has to read as public or nothing
    would be ingested at all.
    """
    name = tag.get("name") or ""
    if name.startswith("#"):
        return False
    visibility = tag.get("visibility")
    return visibility is None or visibility == "public"


def tag_subjects(tags, schemes=WIRED_SCHEMES):
    """Map Ghost tags onto vocabulary subject entries.

    ``tags`` is the ``sort_order``-sorted list of ``{"name", "slug",
    "visibility", "sort_order"}`` dicts that ``ghost_parser._parse_post``
    already builds. Order matters: single-select schemes take the first tag that
    matches them.

    Returns ``(subjects, leftover)`` -- the subject entries for every scheme
    matched, and the display names of the tags no scheme claimed. The leftovers
    are the caller's ``keywords``, which is the designed home for the ~10.6% of
    tag applications that map onto nothing: people, organisations, events and
    sub-national places, none of which have a vocabulary on this profile.

    ``schemes`` is overridable so the dry-run probe can report on a scheme that
    is not wired yet without pretending it is.
    """
    active = [_SCHEMES[name] for name in schemes]
    subjects = []
    claimed_qcodes = {scheme.scheme: set() for scheme in active}
    filled = set()
    leftover = []

    for tag in tags or []:
        name = tag.get("name")
        if not name or not is_public_tag(tag):
            continue

        # Offer both the display name and the slug, as `_parse_language` does:
        # either may be the form that matches ("Afaan Oromo" / "afaan-oromo").
        keys = [
            key for key in (normalise_tag(name), normalise_tag(tag.get("slug"))) if key
        ]
        if not keys or any(key in DROPPED_TAGS for key in keys):
            continue

        matched = False
        for scheme in active:
            if not scheme.multi and scheme.scheme in filled:
                # Single-select and already decided by an earlier tag. Still
                # counts as matched, so a second country tag is not also
                # duplicated into `keywords`.
                if any(scheme.qcode_for(key) for key in keys):
                    matched = True
                continue

            qcode = next(
                (q for q in (scheme.qcode_for(key) for key in keys) if q), None
            )
            if not qcode:
                continue

            matched = True
            if qcode in claimed_qcodes[scheme.scheme]:
                continue
            claimed_qcodes[scheme.scheme].add(qcode)
            subjects.append(scheme.entry(qcode))
            if not scheme.multi:
                filled.add(scheme.scheme)

        if not matched:
            leftover.append(name)

    return subjects, leftover
