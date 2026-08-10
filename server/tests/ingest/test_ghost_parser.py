import json
import os
import tempfile
from unittest.mock import patch

from pesacheck.ingest.ghost_parser import GhostParser
from superdesk.tests import TestCase

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "../fixtures/ghost_export"
)
FIXTURE_PATH = os.path.join(FIXTURE_DIR, "ghost_export.json")


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
        self.assertIn("language", post2)

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
