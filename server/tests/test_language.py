import unittest

from pesacheck import language as language_module
from pesacheck.language import (
    detect_language,
    detect_language_from_tags,
    detect_language_from_text,
    normalise_language_code,
)


# Excerpts from the reference Ghost export in ops/local/ingest/ghost, which is
# the corpus these lists were built and verified against.
ENGLISH_TEXT: str = (
    "This poster claiming Hormuud Telecom is offering instant loans is a HOAX. The advert, "
    "shared on Facebook on 22 June 2026, shows amounts ranging from $50 to $200,000 and "
    "carries the company's logo and colours. However, a reverse image search shows that the "
    "poster was fabricated and the company has confirmed it did not issue any such offer."
)

FRENCH_TEXT: str = (
    "Cette vidéo qui montrerait des soldats maliens tués par l'armée est PARTIELLEMENT FAUSSE. "
    "La séquence, publiée sur les réseaux sociaux, ne montre pas les faits allégués. Selon nos "
    "vérifications, cette image a été prise dans un autre pays et n'a aucun lien avec les "
    "événements décrits par les internautes."
)

SOMALI_TEXT: str = (
    "Xayeysiintan lagu baahiyay Facebook ee lagu sheegay in shirkadda Hormuud ay bixinayso "
    "deyn ayaa ah KHIYAANO. Xayeysiinta oo la baahiyay 22-kii June 2026 ayaa muujinaysa "
    "xaddiga lacagaha, waxaana wehliya astaanta iyo midabada shirkadda. Warbaahinta ayaa "
    "xaqiijiyay in sawirkan been abuur yahay, laakiin qoraalka wali waa la wadaagayaa."
)

OROMO_TEXT: str = (
    "Maxxansi Facebook viidiyoo haleellaa lubbuu galaafate kan ummata Oromoo irratti "
    "raawwatame agarsiisa jedhu kun SOBA. Barreeffamni Afaan Oromoo akkana jedha, garuu "
    "odeeffannoon kun dhugaa hin qabu. Viidiyoon kun naannoo Oromiyaa keessatti waraabame "
    "kan jedhu mirkaneeffame hin jiru, mootummaan waan kana ilaalchisee hin dubbanne."
)

AMHARIC_TEXT: str = (
    "የኢትዮጵያ ኦርቶዶክስ ተዋህዶ ቤተክርስቲያን የሃይማኖት አባት በምርጫ ዘመቻ ላይ ሲሳተፉ ያሳያል በሚል ፌስቡክ ላይ ከምስል ጋር "
    "የተጋራው ይህ ልጥፍ የተቀየረ ነው። ጉግልን በመጠቀም የተደረገው የምስል ፍለጋ በመጣራት ላይ ያለው ፎቶ እንደተቀየረ አረጋግጧል።"
)

# No Kiswahili post exists in the reference export, so this sample is written to
# match the register PesaCheck publishes in rather than lifted from the corpus.
KISWAHILI_TEXT: str = (
    "Chapisho hili la Facebook linalodai kuwa serikali imetangaza siku ya mapumziko ni la "
    "uongo. Uchunguzi wetu umeonyesha kwamba picha hiyo ilikuwa imechapishwa mwaka 2019 na "
    "haihusiani na madai hayo. Taarifa hiyo haikuwa kwenye mitandao ya kijamii ya serikali, "
    "na msemaji alisema hakuna tangazo lililotolewa kuhusu suala hilo."
)


class LanguageFromTagsTestCase(unittest.TestCase):
    def test_detects_each_published_language(self) -> None:
        for tag, expected in [
            ("English", "en"),
            ("Kiswahili", "sw"),
            ("French", "fr"),
            ("Somali", "so"),
            ("Afaan Oromo", "om"),
            ("Amharic", "am"),
        ]:
            with self.subTest(tag=tag):
                self.assertEqual(detect_language_from_tags([(0, tag)]), expected)

    def test_matches_slugs_as_well_as_display_names(self) -> None:
        self.assertEqual(detect_language_from_tags([(0, "afaan-oromo")]), "om")
        self.assertEqual(detect_language_from_tags([(0, "kiswahili")]), "sw")

    def test_matching_ignores_case_and_padding(self) -> None:
        self.assertEqual(detect_language_from_tags([(0, "  FRENCH  ")]), "fr")

    def test_accepts_endonyms(self) -> None:
        self.assertEqual(detect_language_from_tags([(0, "Soomaali")]), "so")
        self.assertEqual(detect_language_from_tags([(0, "Français")]), "fr")

    def test_returns_none_without_a_language_tag(self) -> None:
        self.assertIsNone(detect_language_from_tags([(0, "Kenya"), (1, "Short Form")]))
        self.assertIsNone(detect_language_from_tags([]))

    def test_country_tags_are_not_read_as_languages(self) -> None:
        """The country sitting next to the language tag must not win.

        PesaCheck tags posts with both ``Somali``/``Somalia`` and
        ``French``/``France``, so these pairs are one substring away from
        collapsing into each other.
        """
        for tags, expected in [
            ([(0, "English"), (1, "Somalia")], "en"),
            ([(0, "English"), (1, "France")], "en"),
            ([(0, "English"), (1, "Ethiopia")], "en"),
            ([(0, "Somali"), (1, "Somalia")], "so"),
            ([(0, "French"), (1, "France")], "fr"),
        ]:
            with self.subTest(tags=tags):
                self.assertEqual(detect_language_from_tags(tags), expected)

    def test_bare_oromo_ethnonym_is_not_treated_as_a_language(self) -> None:
        # An English article about Oromo people may carry "Oromo" as a topic
        # tag; only the unambiguous "Afaan Oromo" marks the language.
        self.assertEqual(
            detect_language_from_tags([(0, "English"), (1, "Oromo")]), "en"
        )

    def test_primary_tag_wins_when_two_languages_are_tagged(self) -> None:
        self.assertEqual(
            detect_language_from_tags([(0, "English"), (1, "Amharic")]), "en"
        )
        # Order in the list must not matter — sort_order decides.
        self.assertEqual(
            detect_language_from_tags([(1, "Amharic"), (0, "English")]), "en"
        )

    def test_null_sort_order_is_tolerated(self) -> None:
        self.assertEqual(detect_language_from_tags([(None, "Somali")]), "so")


class LanguageFromTextTestCase(unittest.TestCase):
    def test_detects_each_published_language(self) -> None:
        for text, expected in [
            (ENGLISH_TEXT, "en"),
            (KISWAHILI_TEXT, "sw"),
            (FRENCH_TEXT, "fr"),
            (SOMALI_TEXT, "so"),
            (OROMO_TEXT, "om"),
            (AMHARIC_TEXT, "am"),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(detect_language_from_text(text), expected)

    def test_separates_oromo_from_somali(self) -> None:
        """Both are Cushitic with doubled-vowel Latin spelling; langdetect,
        which has no Oromo model, labels Oromo prose as Somali."""
        self.assertEqual(detect_language_from_text(OROMO_TEXT), "om")
        self.assertEqual(detect_language_from_text(SOMALI_TEXT), "so")

    def test_embedded_english_quotes_do_not_flip_the_result(self) -> None:
        # Fact checks quote the claim being debunked, often in English.
        quoted = (
            SOMALI_TEXT + ' Qoraalka waxaa lagu yiri "Easy application process" iyo '
            '"Instant approval" oo ah hadal been abuur ah.'
        )
        self.assertEqual(detect_language_from_text(quoted), "so")

    def test_a_quoted_amharic_phrase_does_not_make_an_article_amharic(self) -> None:
        # The previous detector returned "am" for any text containing a single
        # Ethiopic character.
        quoted = (
            ENGLISH_TEXT
            + " The post, written in Amharic, reads “የመንግስት ሰራተኞች ደመዎዝ ጥማሪ”."
        )
        self.assertEqual(detect_language_from_text(quoted), "en")

    def test_detects_from_a_headline_alone(self) -> None:
        self.assertEqual(
            detect_language_from_text(
                "FAUX : Cette vidéo ne montre pas des billets dérobés"
            ),
            "fr",
        )
        self.assertEqual(
            detect_language_from_text(
                "BEEN: Muuqaalkan ma aha hub lagu qabtay oo ku yaal Soomaaliya"
            ),
            "so",
        )

    def test_survives_html_markup(self) -> None:
        self.assertEqual(detect_language_from_text("<p>%s</p>" % FRENCH_TEXT), "fr")

    def test_declines_when_no_language_is_clearly_ahead(self) -> None:
        # Function words borrowed from several candidates at once: nothing
        # clears the margin, so the inconclusive branch logs and returns None.
        mixed = "the le kwa oo kun de and ya iyo akka"
        with self.assertLogs(language_module.logger, level="DEBUG"):
            self.assertIsNone(detect_language_from_text(mixed))

    def test_declines_to_guess_without_usable_text(self) -> None:
        for text in [
            "",
            "   ",
            None,
            "Hormuud",
            "2026",
            "$50 200,000 !!!",
            "http://example.com/a",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(detect_language_from_text(text))


class NormaliseLanguageCodeTestCase(unittest.TestCase):
    def test_strips_region_subtags(self) -> None:
        self.assertEqual(normalise_language_code("en-US"), "en")
        self.assertEqual(normalise_language_code("am_ET"), "am")

    def test_maps_names_to_codes(self) -> None:
        self.assertEqual(normalise_language_code("Kiswahili"), "sw")

    def test_passes_through_unsupported_codes(self) -> None:
        # PesaCheck may add a language before this module knows about it.
        self.assertEqual(normalise_language_code("pt-BR"), "pt")

    def test_returns_none_for_empty_values(self) -> None:
        self.assertIsNone(normalise_language_code(None))
        self.assertIsNone(normalise_language_code(""))


class DetectLanguageTestCase(unittest.TestCase):
    def test_tags_take_precedence_over_text(self) -> None:
        self.assertEqual(detect_language([(0, "Kiswahili")], FRENCH_TEXT), "sw")

    def test_falls_back_to_text_when_untagged(self) -> None:
        self.assertEqual(detect_language([(0, "Kenya")], FRENCH_TEXT), "fr")

    def test_falls_back_to_english_when_nothing_is_detectable(self) -> None:
        self.assertEqual(detect_language([], ""), "en")

    def test_default_is_overridable(self) -> None:
        self.assertIsNone(detect_language([], "", default=None))
