import os
from superdesk.tests import TestCase
from superdesk.errors import ParserError
from pesacheck.ingest.medium_parser import MediumParser


class MediumParserTestCase(TestCase):
    def setUp(self):
        super().setUp()

        dirname = os.path.dirname(os.path.realpath(__file__))
        fixture_path = os.path.normpath(
            os.path.join(dirname, "../fixtures/medium_export")
        )
        self.file_path = os.path.join(
            fixture_path,
            "2022-02-27_HOAX--This-UNAIDS-job-advert-in-Uganda-is-fake-f8d269a3d85d.html",
        )
        self.parser = MediumParser()

    async def test_can_parse(self):
        self.assertTrue(self.parser.can_parse(self.file_path))
        self.assertFalse(self.parser.can_parse(__file__))

    async def test_parse_medium_html(self):
        item = await self.parser.parse(self.file_path)

        self.assertEqual(
            item["headline"], "HOAX: This UNAIDS job advert in Uganda is fake"
        )
        self.assertEqual(
            item["abstract"],
            "A UNAIDS Communications officer told PesaCheck that the job advertisement is fake.",
        )
        self.assertTrue(item["body_html"].startswith("<section"))
        self.assertEqual(item["firstcreated"].isoformat(), "2022-02-27T07:26:36.217000")
        self.assertEqual(item["byline"], "PesaCheck")
        self.assertEqual(item["source"], "Medium")

        self.assertEqual(item["slugline"], "HOAX")
        self.assertEqual(
            item["extra"]["original_article_url"],
            "https://medium.com/@PesaCheck/hoax-this-unaids-job-advert-in-uganda-is-fake-f8d269a3d85d",
        )

    async def test_parse_invalid_html(self):
        with self.assertRaises(ParserError) as cm:
            await self.parser.parse("invalid_file_path")
        self.assertEqual(cm.exception.code, 1002)

    async def test_parse_html_without_article(self):
        with open("test_file.html", "w") as f:
            f.write("<html><body>No article here</body></html>")
        try:
            with self.assertRaises(ParserError) as cm:
                await self.parser.parse("test_file.html")
            self.assertEqual(cm.exception.code, 1002)
        finally:
            os.remove("test_file.html")

    async def test_parse_images(self):
        """Test that images are parsed and added to associations."""
        item = await self.parser.parse(self.file_path)

        self.assertIn("associations", item)
        self.assertIn("featuremedia", item["associations"])

        featuremedia = item["associations"]["featuremedia"]
        self.assertEqual(featuremedia["type"], "picture")

        embedded_count = sum(
            1 for key in item["associations"].keys() if key.startswith("embedded")
        )
        self.assertGreater(embedded_count, 0)

        # verify the image GUID is generated
        self.assertTrue(featuremedia["guid"].endswith("-image"))

        # verify required fields are present
        self.assertIn("headline", featuremedia)
        self.assertIn("alt_text", featuremedia)
        self.assertIn("description_text", featuremedia)
