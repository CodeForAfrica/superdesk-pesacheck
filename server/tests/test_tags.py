import unittest

from pesacheck import tag_vocabularies as tv
from pesacheck.debunk import _RATING_NAMES, DEBUNK_SCHEME
from pesacheck.tags import (
    _SCHEMES,
    CLAIM_TOPIC,
    CONTENT_TYPE,
    COUNTRIES_SCHEME,
    DEBUNK_LANGUAGE,
    GEC,
    PLATFORM,
    PRIMARY_COUNTRY,
    WIRED_SCHEMES,
    is_public_tag,
    normalise_tag,
    tag_subjects,
)

from tests.content_config import (
    CONTENT_CONFIG_ARCHIVE,
    allowed_subject_schemes,
    names_by_qcode,
    qcodes,
)


def tags(*names, visibility="public"):
    """Build the tag dicts `ghost_parser._parse_post` passes to `tag_subjects`."""
    return [
        {
            "name": name,
            "slug": name.lower().replace(" ", "-"),
            "visibility": visibility,
            "sort_order": order,
        }
        for order, name in enumerate(names)
    ]


def schemes_in(subjects):
    return {entry["scheme"] for entry in subjects}


def qcode_for(subjects, scheme):
    found = [e["qcode"] for e in subjects if e["scheme"] == scheme]
    return found[0] if len(found) == 1 else found


class VocabularyConformanceTestCase(unittest.TestCase):
    """Every qcode this repo emits must exist in the vocabulary that defines it.

    The whole tag mapping keys off vocabularies this repo does not own:
    `superdesk-content-config.tgz` is a periodically re-exported UAT mongodump
    and `bootstrap_superdesk.py` restores `vocabularies` from it wholesale. A
    refresh that renames a qcode orphans every item carrying it, and the symptom
    -- a field that renders blank -- shows up nowhere near the cause.

    This is the only thing that turns that from a silent breakage into a failing
    test, which is why it is the first test in the file. When it fails, the fix
    is in `tag_vocabularies.py` (or `debunk.py`), not here.
    """

    def test_every_scheme_is_a_real_vocabulary(self) -> None:
        for scheme in _SCHEMES:
            with self.subTest(scheme=scheme):
                # Raises with a pointed message if the vocabulary is gone.
                self.assertTrue(qcodes(scheme))

    def test_every_mapped_qcode_exists_in_its_vocabulary(self) -> None:
        for scheme, mapping in _SCHEMES.items():
            live = qcodes(scheme)
            orphans = sorted(set(mapping.lookup.values()) - live)
            with self.subTest(scheme=scheme):
                self.assertEqual(
                    orphans,
                    [],
                    f"{scheme}: {len(orphans)} qcode(s) in tag_vocabularies.py are "
                    f"absent from the {scheme} vocabulary in "
                    f"{CONTENT_CONFIG_ARCHIVE.name}: {orphans}. Either the dump was "
                    "refreshed and dropped them, or the mapping invented them.",
                )

    def test_every_debunk_rating_qcode_exists_in_its_vocabulary(self) -> None:
        # debunk.py predates this test, and this is the defect that motivated
        # writing it: it mapped ten ratings against a seven-item vocabulary, so
        # 2.4% of rated items carried an unresolvable qcode. Three of the ten
        # are supplied by bootstrap_superdesk.py's VOCABULARY_ITEM_ADDITIONS,
        # which `qcodes()` folds in -- so this passing depends on that override
        # still being applied.
        orphans = sorted(set(_RATING_NAMES) - qcodes(DEBUNK_SCHEME))
        self.assertEqual(
            orphans,
            [],
            f"debunk.py maps {orphans} which the Debunk vocabulary does not define.",
        )

    def test_stored_display_names_match_the_vocabulary(self) -> None:
        # The subject entry carries the label so the client renders it without a
        # vocabulary lookup, which means a stale label here shows the wrong text
        # rather than failing loudly.
        for scheme, mapping in _SCHEMES.items():
            live = names_by_qcode(scheme)
            wrong = {
                qcode: (stored, live.get(qcode))
                for qcode, stored in mapping.names_by_qcode.items()
                if qcode in live and stored != live[qcode]
            }
            with self.subTest(scheme=scheme):
                self.assertEqual(wrong, {}, f"{scheme}: stale display names {wrong}")

    def test_debunk_rating_names_match_the_vocabulary(self) -> None:
        live = names_by_qcode(DEBUNK_SCHEME)
        wrong = {
            qcode: (stored, live.get(qcode))
            for qcode, stored in _RATING_NAMES.items()
            if qcode in live and stored != live[qcode]
        }
        self.assertEqual(wrong, {})

    def test_every_scheme_is_allowed_on_the_article_profile(self) -> None:
        # A different failure from an unknown qcode, and a louder one: core
        # validates every subject entry's `scheme` against this whitelist, so an
        # entry naming a scheme outside it is rejected rather than mislabelled.
        allowed = allowed_subject_schemes("Article")
        self.assertEqual(sorted(set(_SCHEMES) - allowed), [])
        self.assertIn(DEBUNK_SCHEME, allowed)

    def test_primary_country_is_a_subset_of_countries(self) -> None:
        # The two country schemes share one alias table because they share one
        # qcode space (ISO 3166-1 alpha-3). If that stopped being true, the
        # shared table would start emitting codes one of them cannot resolve.
        self.assertLessEqual(set(tv.PRIMARY_COUNTRIES), set(tv.COUNTRIES))

    def test_dr_congo_agrees_across_both_country_schemes(self) -> None:
        # Phase 0.2: countrymention1 shipped DR Congo as COG, which is
        # Congo-Brazzaville. bootstrap_superdesk.py repairs it to COD, and the
        # alias table maps every Ghost spelling of the DRC onto COD.
        self.assertEqual(tv.PRIMARY_COUNTRIES["COD"], "DR Congo")
        self.assertNotIn("COG", tv.PRIMARY_COUNTRIES)
        subjects, _ = tag_subjects(tags("Democratic Republic Congo"))
        self.assertEqual(qcode_for(subjects, PRIMARY_COUNTRY), "COD")
        self.assertEqual(qcode_for(subjects, COUNTRIES_SCHEME), "COD")

    def test_bare_congo_resolves_to_kinshasa_in_both_schemes(self) -> None:
        # 197 applications across the corpus, 193 of them with no other Congo
        # spelling on the post. `countrymention1` has no Brazzaville item, so
        # sending this anywhere but COD would either orphan the qcode or make
        # the two country fields disagree.
        self.assertNotIn(
            "COG", {q for q, n in tv.PRIMARY_COUNTRIES.items() if "Congo" in n}
        )
        subjects, leftover = tag_subjects(tags("Congo"))
        self.assertEqual(qcode_for(subjects, PRIMARY_COUNTRY), "COD")
        self.assertEqual(qcode_for(subjects, COUNTRIES_SCHEME), "COD")
        self.assertEqual(leftover, [])

    def test_mojibake_is_repaired_before_it_is_stored(self) -> None:
        # Phase 0.3: the dump spells this "CÃ´te d'Ivoire".
        self.assertEqual(tv.COUNTRIES["CIV"], "Côte d'Ivoire")
        subjects, _ = tag_subjects(tags("Ivory Coast"))
        entry = [e for e in subjects if e["scheme"] == COUNTRIES_SCHEME][0]
        self.assertEqual(entry["name"], "Côte d'Ivoire")


class NormaliseTagTestCase(unittest.TestCase):
    def test_collapses_accents_case_and_punctuation(self) -> None:
        for text in ["Côte d'Ivoire", "COTE D'IVOIRE", "Cote d Ivoire", "côte-divoire"]:
            with self.subTest(text=text):
                self.assertEqual(normalise_tag(text), "cotedivoire")

    def test_returns_empty_for_non_strings_and_blanks(self) -> None:
        for value in [None, 0, [], "", "   ", "!!!"]:
            with self.subTest(value=value):
                self.assertEqual(normalise_tag(value), "")

    def test_folds_fullwidth_and_compatibility_forms(self) -> None:
        # NFKD, not NFD -- exports carry fullwidth characters, as debunk.py's
        # prefix regex also has to account for.
        self.assertEqual(normalise_tag("Ｋｅｎｙａ"), "kenya")


class TagSubjectsTestCase(unittest.TestCase):
    def test_maps_a_representative_post(self) -> None:
        subjects, leftover = tag_subjects(
            tags("Short Form", "English", "Kenya", "Elections", "Raila Odinga")
        )
        self.assertEqual(
            {(e["scheme"], e["qcode"]) for e in subjects},
            {
                (CONTENT_TYPE, "quickread"),
                (DEBUNK_LANGUAGE, "debunkeng"),
                (PRIMARY_COUNTRY, "KEN"),
                (COUNTRIES_SCHEME, "KEN"),
                (CLAIM_TOPIC, "elections"),
            },
        )
        self.assertEqual(leftover, ["Raila Odinga"])

    def test_single_select_takes_the_first_tag_by_sort_order(self) -> None:
        subjects, leftover = tag_subjects(tags("Uganda", "Kenya"))
        self.assertEqual(qcode_for(subjects, PRIMARY_COUNTRY), "UGA")
        # Nothing is lost: the multi-select scheme still gets both.
        self.assertEqual(
            sorted(e["qcode"] for e in subjects if e["scheme"] == COUNTRIES_SCHEME),
            ["KEN", "UGA"],
        )
        # And the loser is not also duplicated into keywords.
        self.assertEqual(leftover, [])

    def test_sort_order_decides_which_country_is_primary(self) -> None:
        reversed_tags = tags("Kenya", "Uganda")
        subjects, _ = tag_subjects(reversed_tags)
        self.assertEqual(qcode_for(subjects, PRIMARY_COUNTRY), "KEN")

    def test_multi_select_takes_every_match(self) -> None:
        subjects, _ = tag_subjects(tags("Elections", "Politics", "Health"))
        self.assertEqual(
            sorted(e["qcode"] for e in subjects if e["scheme"] == CLAIM_TOPIC),
            ["elections", "health", "politics"],
        )

    def test_one_tag_populates_every_scheme_it_matches(self) -> None:
        # `France` is both a country and a GEC category. Subject entries are
        # scheme-scoped, so this is not a collision -- and it is why GEC being
        # wired is a scope decision rather than a technical one.
        subjects, leftover = tag_subjects(
            tags("France"), schemes=WIRED_SCHEMES + (GEC,)
        )
        self.assertEqual(
            {(e["scheme"], e["qcode"]) for e in subjects},
            {(COUNTRIES_SCHEME, "FRA"), (GEC, "france")},
        )
        self.assertEqual(leftover, [])

    def test_gec_is_not_wired_by_default(self) -> None:
        self.assertNotIn(GEC, WIRED_SCHEMES)
        subjects, _ = tag_subjects(tags("France"))
        self.assertNotIn(GEC, schemes_in(subjects))

    def test_non_african_country_fills_countries_but_not_primary(self) -> None:
        # `countrymention1` lists only the 53 African countries, so a Russia tag
        # must not be offered to it -- the qcode would not resolve.
        subjects, leftover = tag_subjects(tags("Russia"))
        self.assertEqual(qcode_for(subjects, COUNTRIES_SCHEME), "RUS")
        self.assertNotIn(PRIMARY_COUNTRY, schemes_in(subjects))
        self.assertEqual(leftover, [])

    def test_non_african_tag_does_not_consume_the_primary_slot(self) -> None:
        subjects, _ = tag_subjects(tags("Russia", "Mali"))
        self.assertEqual(qcode_for(subjects, PRIMARY_COUNTRY), "MLI")

    def test_matches_on_the_slug_when_the_name_does_not(self) -> None:
        subjects, _ = tag_subjects(
            [{"name": "Afaan Oromoo", "slug": "afaan-oromo", "sort_order": 0}]
        )
        self.assertEqual(qcode_for(subjects, DEBUNK_LANGUAGE), "debunkoro")

    def test_short_form_is_quick_read_and_long_form_is_its_own_item(self) -> None:
        # Editorial decision, 2026-08-28: Long Form is deliberately NOT
        # Explainer, and bootstrap_superdesk.py adds the matching item.
        self.assertEqual(
            qcode_for(tag_subjects(tags("Short Form"))[0], CONTENT_TYPE), "quickread"
        )
        self.assertEqual(
            qcode_for(tag_subjects(tags("Long Form"))[0], CONTENT_TYPE), "longform"
        )

    def test_twitter_maps_onto_the_x_twitter_item(self) -> None:
        subjects, _ = tag_subjects(tags("Twitter"))
        entry = [e for e in subjects if e["scheme"] == PLATFORM][0]
        self.assertEqual((entry["qcode"], entry["name"]), ("xtwit", "X/Twitter"))

    def test_duplicate_tags_yield_one_entry(self) -> None:
        subjects, leftover = tag_subjects(tags("Kenya", "KENYA", "kenya"))
        self.assertEqual(
            [e["qcode"] for e in subjects if e["scheme"] == COUNTRIES_SCHEME], ["KEN"]
        )
        self.assertEqual(leftover, [])

    def test_unmatched_tags_become_keywords_in_order(self) -> None:
        _, leftover = tag_subjects(
            tags("Bobi Wine", "Kenya", "Horn Of Africa", "Coca Cola")
        )
        self.assertEqual(leftover, ["Bobi Wine", "Horn Of Africa", "Coca Cola"])

    def test_self_referential_tags_are_dropped_entirely(self) -> None:
        subjects, leftover = tag_subjects(
            tags("Fact Checking", "Misinformation", "Fake News", "Kenya")
        )
        self.assertEqual(leftover, [])
        self.assertEqual(qcode_for(subjects, COUNTRIES_SCHEME), "KEN")

    def test_handles_empty_and_malformed_input(self) -> None:
        for value in [None, [], [{}], [{"name": None, "slug": None}]]:
            with self.subTest(value=value):
                self.assertEqual(tag_subjects(value), ([], []))

    def test_every_entry_has_name_qcode_and_scheme(self) -> None:
        subjects, _ = tag_subjects(
            tags("Short Form", "Somali", "Somalia", "Twitter", "Politics")
        )
        self.assertTrue(subjects)
        for entry in subjects:
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), {"name", "qcode", "scheme"})
                self.assertTrue(entry["name"])
                self.assertTrue(entry["qcode"])


class InternalTagTestCase(unittest.TestCase):
    def test_internal_visibility_is_excluded(self) -> None:
        subjects, leftover = tag_subjects(
            tags("#Import 2025-11-27 17:24", visibility="internal")
        )
        self.assertEqual((subjects, leftover), ([], []))

    def test_hash_prefixed_name_is_excluded_without_visibility(self) -> None:
        # Older export files omit the `visibility` column entirely, so the name
        # is the only signal left.
        self.assertFalse(is_public_tag({"name": "#Import 2025-11-27 17:24"}))

    def test_missing_visibility_reads_as_public(self) -> None:
        # A missing value must not exclude the tag, or an older export ingests
        # with no tags mapped at all.
        self.assertTrue(is_public_tag({"name": "Kenya"}))
        subjects, _ = tag_subjects(
            [{"name": "Kenya", "slug": "kenya", "sort_order": 0}]
        )
        self.assertEqual(qcode_for(subjects, COUNTRIES_SCHEME), "KEN")

    def test_internal_tags_do_not_consume_a_single_select_slot(self) -> None:
        subjects, _ = tag_subjects(
            tags("#Import 2025-11-27 17:24", visibility="internal") + tags("Kenya")
        )
        self.assertEqual(qcode_for(subjects, PRIMARY_COUNTRY), "KEN")
