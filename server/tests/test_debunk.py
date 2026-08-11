import unittest

from pesacheck.debunk import DEBUNK_SCHEME, debunk_rating


class DebunkRatingTestCase(unittest.TestCase):
    # Headline prefixes taken from the reference Ghost export in
    # ops/local/ingest/ghost, one per language/verdict combination seen there.
    def test_maps_each_known_prefix_to_its_qcode(self) -> None:
        for title, qcode in [
            (
                "ALTERED: This 13 July 2026 cover of The Standard is manipulated",
                "altered",
            ),
            ("FALSE: This video does not show President Ruto", "false"),
            ("HOAX: This poster is a scam", "hoax"),
            ("PARTLY FALSE: This video does not show UPDF soldiers", "partfalse"),
            ("FAUX : Cette photo ne montre pas le bombardement", "false"),
            ("CANULAR : Ce recrutement n’émane pas du Fonds", "hoax"),
            ("PARTIELLEMENT FAUX : Cette vidéo ne montre pas", "partfalse"),
            ("MANIPULÉE : Cette première page est un montage", "altered"),
            ("CONTEXTE MANQUANT : Cette vidéo ne date pas", "context"),
            ("KHIYAANO: Xayeysiintan waa khiyaano", "hoax"),
            ("WAX LAGA BEDALAY: Muuqaalkan ma aha", "altered"),
            ("BEEN: Sheegashadan waa been", "false"),
            ("SOBA: Viidiyoon kun haleellaa hin agarsiisu", "false"),
            ("የተቀየረ: ይህ ምስል የኢትዮጵያ ኦርቶዶክስ ተዋህዶ ቤተ", "altered"),
            ("ሐሰት: ይህ የመንግስት ሰራተኞችን የደሞዝ ጭማሪ", "false"),
            ("በከፊል ሐሰት: ይህ ምስል በሐምሌ ወር", "partfalse"),
        ]:
            with self.subTest(title=title):
                rating = debunk_rating(title)
                self.assertIsNotNone(rating)
                self.assertEqual(rating["qcode"], qcode)
                self.assertEqual(rating["scheme"], DEBUNK_SCHEME)
                self.assertTrue(rating["name"])

    # Verdict prefixes drawn from the full pesacheck.org corpus (all published
    # posts), covering every language and folding synonyms/typos onto the right
    # qcode. One representative per (language, verdict) pair.
    def test_maps_corpus_prefixes_to_qcodes(self) -> None:
        for title, qcode in [
            # English synonyms + the four new categories
            ("DOCTORED: This tweet is fabricated", "altered"),
            ("SATIRICAL: This directive is fabricated", "satire"),
            ("MISLEADING: This video does not show bandits", "misleading"),
            ("SCAM: This website claiming jobs is a hoax", "scam"),
            ("TRUE: Wildfire breaks out on Mount Kilimanjaro", "true"),
            ("MIXTURE: The World Bank did not fully withdraw", "mixture"),
            # French
            ("INTOX : La Russie n’a pas bombardé le palais", "false"),
            ("FAUSSE : Cette vidéo ne montre pas un incendie", "false"),
            ("MANIPULÉ : Ce communiqué est un faux", "altered"),
            ("TRUQUÉE : Cette photo a été modifiée", "altered"),
            ("MODIFIÉE : Cette vidéo a été altérée", "altered"),
            ("RETOUCHÉE : La photo a été modifiée", "altered"),
            ("HORS CONTEXTE : Cette vidéo ne date pas de 2024", "context"),
            ("SATIRIQUE : Cette photo ne montre pas", "satire"),
            ("TROMPEUR : Cette marque n’est pas affiliée", "misleading"),
            ("VRAI : Cet avis de recrutement est authentique", "true"),
            ("FAUX TITRE : L’OMS a reconnu 150 vaccins", "falsehead"),
            # Kiswahili (previously unmapped entirely)
            ("SI KWELI: Video hii haionyeshi ajali", "false"),
            ("UONGO: Akaunti hii ni feki", "false"),
            ("IMEBADILISHWA: Video hii imehaririwa", "altered"),
            ("KICHWA CHA HABARI POTOFU: Video hii", "falsehead"),
            ("UONGO KWA SEHEMU: Picha hii", "partfalse"),
            ("GHUSHI: Barua hii inayodaiwa", "hoax"),
            ("FEKI: Taarifa hii haitoki CHADEMA", "hoax"),
            ("UZUSHI: Tangazo hili la nafasi za kazi ni feki", "hoax"),
            # Somali
            ("QEYB AHAAN BEEN AH: Sawirkan ma aha", "partfalse"),
            ("BEEN AH: Sawirradan ma aha markab", "false"),
            ("BEEN ABUUR: Waa been qoraalkan", "hoax"),
            ("MAADEYSI: Waa been abuur muuqaalkan", "satire"),
            ("HAREERMARSAN XAQIIQDA: Muuqaalkan", "misleading"),
            # Afaan Oromo
            ("GAR-TOKKOON SOBA: Suuraaleen kunneen", "partfalse"),
            ("DHUGAA MITTI : Suuraan kun kan Ameerikaa miti", "false"),
            ("HAALAAN ALA: Suuraan kun haleellaa", "context"),
            ("KAN JIJJIIRAME: suuraan mormii", "altered"),
            ("AFAANFAAJJESSA: Maxxansi kun", "misleading"),
            # Amharic — delimited by the Ethiopic wordspace ``፡``, not ``:``
            ("ከአውድ ውጪ፡ ይህ ምስል በሐምሌ ወር", "context"),
            ("ሐሰተኛ አርዕስት፡ እዚህ ቪዲዮ ላይ", "falsehead"),
            ("ስላቅ፡ ይህ ምስል የብልጽግና ፓርቲ", "satire"),
            ("የፈጠራ ወሬ፡ የየመን ሚሳኤል", "hoax"),
            ("የተጭበረበረ፡ ይህ የቴሌግራም ቻናል", "hoax"),
        ]:
            with self.subTest(title=title):
                rating = debunk_rating(title)
                self.assertIsNotNone(rating)
                self.assertEqual(rating["qcode"], qcode)
                self.assertEqual(rating["scheme"], DEBUNK_SCHEME)
                self.assertTrue(rating["name"])

    def test_ethiopic_wordspace_delimits_the_verdict(self) -> None:
        # Amharic headlines set the verdict off with ``፡`` (U+1361), not ``:``;
        # the already-mapped ratings must resolve with that delimiter too.
        self.assertEqual(debunk_rating("ሐሰት፡ ይህ ቪዲዮ አያሳይም")["qcode"], "false")
        self.assertEqual(debunk_rating("በከፊል ሐሰት፡ ይህ ምስል")["qcode"], "partfalse")
        self.assertEqual(debunk_rating("የተቀየረ፡ ይህ ቪዲዮ ተለውጧል")["qcode"], "altered")

    def test_ethiopic_semicolon_and_comma_delimit_the_verdict(self) -> None:
        # The export also sets the verdict off with the Ethiopic semicolon ``፤``
        # (U+1364) and comma ``፣`` (U+1363), sometimes with a space before it.
        self.assertEqual(debunk_rating("የተጭበረበረ፤ ካናዳ ኤምባሲ")["qcode"], "hoax")
        self.assertEqual(debunk_rating("የተቀየረ ፤ ይህ ጽሁፍ")["qcode"], "altered")
        self.assertEqual(debunk_rating("በከፊል ሐሰተኛ፣ ይህ ቪዲዮ")["qcode"], "partfalse")

    def test_wishet_is_a_synonym_for_false(self) -> None:
        # ``ውሸት`` is an Amharic synonym of ``ሐሰት`` (false).
        self.assertEqual(debunk_rating("ውሸት፡ ይህ ምስል")["qcode"], "false")
        self.assertEqual(debunk_rating("በከፊል ውሸት፡ ይህ ምስል")["qcode"], "partfalse")

    def test_fake_maps_to_hoax(self) -> None:
        # "Fake" has no exact Debunk rating; the posts it prefixes are scams.
        self.assertEqual(debunk_rating("FAKE: This poster is a scam")["qcode"], "hoax")

    def test_prefix_matching_ignores_case_and_padding(self) -> None:
        self.assertEqual(
            debunk_rating("  partly   false : claim")["qcode"], "partfalse"
        )

    def test_accepts_fullwidth_colon(self) -> None:
        self.assertEqual(debunk_rating("HOAX：claim")["qcode"], "hoax")

    def test_unknown_prefix_leaves_rating_unset(self) -> None:
        self.assertIsNone(debunk_rating("UPDATE: the situation has changed"))

    def test_headline_without_a_prefix_leaves_rating_unset(self) -> None:
        self.assertIsNone(debunk_rating("This video does not show what it claims"))

    def test_empty_headline_leaves_rating_unset(self) -> None:
        self.assertIsNone(debunk_rating(""))
        self.assertIsNone(debunk_rating(None))

    def test_a_colon_deep_in_a_sentence_is_not_read_as_a_verdict(self) -> None:
        self.assertIsNone(
            debunk_rating(
                "This long headline runs well past forty characters before it "
                "finally reaches a colon: and so has no verdict prefix"
            )
        )


if __name__ == "__main__":
    unittest.main()
