import json
import os
import tempfile
from unittest.mock import patch

from pesacheck.ingest.ghost_parser import GhostParser
from pesacheck.language import SUPPORTED_LANGUAGES
from superdesk.tests import TestCase

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "../fixtures/ghost_export"
)
FIXTURE_PATH = os.path.join(FIXTURE_DIR, "ghost_export.json")
LANGUAGES_FIXTURE_PATH = os.path.join(FIXTURE_DIR, "ghost_export_languages.json")


def _mock_update_renditions(item, url, old_item, **kwargs):
    """Stub that records the source URL without making HTTP requests."""
    item["renditions"] = {"original": {"href": url, "mimetype": "image/jpeg"}}


class GhostParserTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.parser = GhostParser()

    # ------------------------------------------------------------------
    # can_parse
    # ------------------------------------------------------------------

    async def test_can_parse_valid_file(self):
        self.assertTrue(self.parser.can_parse(FIXTURE_PATH))

    async def test_can_parse_rejects_non_ghost_file(self):
        self.assertFalse(self.parser.can_parse(__file__))

    async def test_can_parse_rejects_missing_file(self):
        self.assertFalse(self.parser.can_parse("/nonexistent/file.json"))

    # ------------------------------------------------------------------
    # parse — field mapping
    # ------------------------------------------------------------------

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_returns_only_published_posts(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        # draft (post_003) and page (post_004) should be excluded
        self.assertEqual(len(items), 2)

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_headline_and_slugline(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertEqual(post1["headline"], "FAUX: This claim is false")
        self.assertEqual(post1["slugline"], "faux-this-claim-is-false")

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_abstract(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertEqual(post1["abstract"], "A short description of the article.")

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_body_html(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertIn("<p>This is the article body.</p>", post1["body_html"])

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_dates(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertEqual(post1["firstcreated"].isoformat(), "2025-11-01T10:00:00+00:00")
        self.assertEqual(
            post1["versioncreated"].isoformat(), "2025-11-01T11:00:00+00:00"
        )

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_source(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        for item in items:
            self.assertEqual(item["source"], "Ghost")

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_locale_mapped_to_language(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertEqual(post1["language"], "fr")

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_null_locale_guesses_language(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post2 = next(
            i for i in items if i["guid"] == "bbbbbbbb-0002-0002-0002-bbbbbbbbbbbb"
        )
        self.assertEqual(post2["language"], "en")

    # ------------------------------------------------------------------
    # parse — language detection
    # ------------------------------------------------------------------

    async def _languages_by_guid(self):
        items = await self.parser.parse(LANGUAGES_FIXTURE_PATH)
        return {item["guid"]: item["language"] for item in items}

    async def test_parse_language_from_tag(self):
        languages = await self._languages_by_guid()
        self.assertEqual(languages["11111111-0001-0001-0001-111111111111"], "sw")
        self.assertEqual(languages["33333333-0003-0003-0003-333333333333"], "om")
        self.assertEqual(languages["66666666-0006-0006-0006-666666666666"], "en")

    async def test_parse_language_falls_back_to_text_when_untagged(self):
        # lang_002 predates the language-tag convention: only "Somalia" and
        # "Short Form" are tagged, so the body has to carry the language.
        languages = await self._languages_by_guid()
        self.assertEqual(languages["22222222-0002-0002-0002-222222222222"], "so")

    async def test_parse_language_falls_back_to_text_without_any_tags(self):
        languages = await self._languages_by_guid()
        self.assertEqual(languages["55555555-0005-0005-0005-555555555555"], "am")

    async def test_parse_language_prefers_locale_and_normalises_it(self):
        # locale "en-US" outranks the French tag and is reduced to "en".
        languages = await self._languages_by_guid()
        self.assertEqual(languages["44444444-0004-0004-0004-444444444444"], "en")

    async def test_parse_language_ignores_adjacent_country_tag(self):
        # lang_006 is tagged English + Somalia; "Somalia" must not read as Somali.
        languages = await self._languages_by_guid()
        self.assertEqual(languages["66666666-0006-0006-0006-666666666666"], "en")

    async def test_parse_language_set_on_every_item(self):
        items = await self.parser.parse(LANGUAGES_FIXTURE_PATH)
        self.assertEqual(len(items), 6)
        for item in items:
            self.assertIn(item["language"], SUPPORTED_LANGUAGES)

    # ------------------------------------------------------------------
    # parse — authors → byline
    # ------------------------------------------------------------------

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_byline_multiple_authors_sorted(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertEqual(post1["byline"], "Alice Reporter, Bob Editor")

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_byline_single_author(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post2 = next(
            i for i in items if i["guid"] == "bbbbbbbb-0002-0002-0002-bbbbbbbbbbbb"
        )
        self.assertEqual(post2["byline"], "Alice Reporter")

    # ------------------------------------------------------------------
    # parse — tags → keywords
    # ------------------------------------------------------------------

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_keywords(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertEqual(post1["keywords"], ["Fact Check", "Africa"])

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_keywords_single_tag(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post2 = next(
            i for i in items if i["guid"] == "bbbbbbbb-0002-0002-0002-bbbbbbbbbbbb"
        )
        self.assertEqual(post2["keywords"], ["Fact Check"])

    # ------------------------------------------------------------------
    # parse — images
    # ------------------------------------------------------------------

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_feature_image(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        self.assertIn("associations", post1)
        featuremedia = post1["associations"]["featuremedia"]
        self.assertEqual(featuremedia["type"], "picture")
        self.assertTrue(featuremedia["guid"].endswith("-image"))
        self.assertEqual(
            featuremedia["renditions"]["original"]["href"],
            "https://example.com/feature.jpg",
        )

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_inline_image_added_as_embedded(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post1 = next(
            i for i in items if i["guid"] == "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
        )
        associations = post1["associations"]
        embedded_keys = [k for k in associations if k.startswith("embedded")]
        self.assertEqual(len(embedded_keys), 1)
        embedded = associations[embedded_keys[0]]
        self.assertEqual(
            embedded["renditions"]["original"]["href"], "https://example.com/inline.jpg"
        )
        self.assertEqual(embedded["alt_text"], "An inline image")
        self.assertEqual(embedded["description_text"], "Image caption here")

    @patch(
        "pesacheck.ingest.ghost_parser.update_renditions",
        side_effect=_mock_update_renditions,
    )
    async def test_parse_no_feature_image_no_associations(self, _mock):
        items = await self.parser.parse(FIXTURE_PATH)
        post2 = next(
            i for i in items if i["guid"] == "bbbbbbbb-0002-0002-0002-bbbbbbbbbbbb"
        )
        self.assertNotIn("associations", post2)

    # ------------------------------------------------------------------
    # parse — error handling
    # ------------------------------------------------------------------

    async def test_parse_invalid_path_raises_parser_error(self):
        from superdesk.errors import ParserError

        with self.assertRaises(ParserError):
            await self.parser.parse("/nonexistent/file.json")

    async def test_parse_invalid_json_raises_parser_error(self):
        from superdesk.errors import ParserError

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("not valid json")
            tmp_path = f.name
        try:
            with self.assertRaises(ParserError):
                await self.parser.parse(tmp_path)
        finally:
            os.unlink(tmp_path)

    async def test_parse_missing_db_key_raises_parser_error(self):
        from superdesk.errors import ParserError

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"not_db": []}, f)
            tmp_path = f.name
        try:
            with self.assertRaises(ParserError):
                await self.parser.parse(tmp_path)
        finally:
            os.unlink(tmp_path)
