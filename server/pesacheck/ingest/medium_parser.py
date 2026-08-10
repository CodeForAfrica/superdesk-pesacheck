import hashlib
import arrow
import logging

from datetime import datetime

from superdesk.utc import utcnow
from superdesk.errors import ParserError
from superdesk.etree import parse_html, to_string
from superdesk.metadata.utils import generate_guid
from superdesk.io.feed_parsers import FileFeedParser
from superdesk.io.registry import register_feed_parser
from superdesk.media.renditions import update_renditions
from superdesk.metadata.item import (
    FORMAT,
    GUID_FIELD,
    GUID_TAG,
    ITEM_TYPE,
    CONTENT_TYPE,
    FORMATS,
)


logger = logging.getLogger(__name__)


class MediumParser(FileFeedParser):
    """
    Feed Parser for parsing Medium export files.
    """

    NAME = "medium"
    label = "Medium Parser"

    def parse_publish_date(self, date_string):
        """Parse Medium's published datetime string into datetime object.

        :param date_string: datetime string from Medium's dt-published field
        :return: datetime object
        :raises ValueError: if date_string cannot be parsed
        """
        try:
            return datetime.strptime(date_string, "%Y-%m-%dT%H:%M:%S.%fZ")
        except (ValueError, TypeError):
            try:
                return arrow.get(date_string.strip()).datetime
            except arrow.parser.ParserError:
                raise ValueError(date_string.strip())

    def can_parse(self, file_path):
        """
        Determines if the given file is a Medium export file by checking:
        1. The unique export footer text in the footer element
        2. The presence of data-field="body" attribute

        :param file_path: Path to the file to check
        :return: True if the file is a Medium export, False otherwise
        """
        try:
            with open(file_path, "rb") as f:
                html_content = f.read().decode("utf-8")
                root = parse_html(html_content, "html")

                exported_from_medium = (
                    'Exported from <a href="https://medium.com">Medium</a>'
                    in to_string(root.find(".//footer"), method="html")
                )
                article_body_found = (
                    root.find(".//section[@data-field='body']") is not None
                )

                return exported_from_medium and article_body_found
        except Exception:
            return False

    def _get_text(self, element):
        if element is None:
            return ""

        if element.text is None:
            return ""

        return element.text.strip()

    def _generate_image_guid(self, url):
        """Generate a GUID for the given image url"""

        guid_hash = hashlib.sha1(url.encode("utf8")).hexdigest()
        return generate_guid(type=GUID_TAG, id=guid_hash + "-image")

    def _add_image(
        self, item, url, alt_text="", description_text="", is_featured=False
    ):
        """Add an image to the item's associations.

        :param item: The item dictionary to add the image to
        :param url: The URL of the image
        :param alt_text: Alt text for the image
        :param description_text: Description text for the image
        :param is_featured: Whether this image is marked as featured
        """

        associations = item.setdefault("associations", {})
        association = {
            ITEM_TYPE: CONTENT_TYPE.PICTURE,
            GUID_FIELD: self._generate_image_guid(url),
            "headline": item["headline"],
            "alt_text": alt_text,
            "description_text": description_text,
        }
        update_renditions(association, url, None)

        if is_featured and "featuremedia" not in associations:
            key = "featuremedia"
        elif "featuremedia" not in associations:
            key = "featuremedia"
        else:
            key = "embedded" + str(len(associations) - 1)

        associations[key] = association

    def parse_images(self, item, article):
        """Parse images from the article content and add them to associations.

        :param item: The item dictionary to add images to
        :param article: The article element containing the content
        """
        for img in article.xpath(".//img"):
            try:
                src = img.get("src")
                if not src:
                    continue

                alt_text = img.get("alt", "")
                description_text = ""

                parent = img.getparent()
                if parent is not None and parent.tag == "figure":
                    figcaption = parent.find(".//figcaption")
                    if figcaption is not None:
                        description_text = self._get_text(figcaption) or ""

                is_featured = img.get("data-is-featured") == "true"
                self._add_image(item, src, alt_text, description_text, is_featured)

            except Exception as e:
                logger.warning(
                    f"Failed to parse image {img.get('src', 'unknown')}: {e}"
                )
                continue

    async def parse(self, file_path, provider=None):
        """
        Parse the given file and return the item for further processing.
        :param file_path: Path to the file to parse
        :param provider: Ingest Provider Details
        :return: Item to be processed
        """
        try:
            with open(file_path, "rb") as f:
                html_content = f.read().decode("utf-8")

            root = parse_html(html_content, "html")

            article = root.find(".//section[@data-field='body']")
            if article is None:
                raise ValueError("Could not find article content")

            title = root.find(".//h1[@class='p-name']")
            if title is None or title.text is None:
                raise ValueError("Could not find article title")

            abstract = root.find(".//section[@data-field='subtitle']")

            # author is always PesaCheck but will leave this just in case
            author = root.find(".//footer//a[@class='p-author h-card']")

            date = root.find(".//time[@class='dt-published']")
            if date is not None:
                firstcreated = self.parse_publish_date(date.get("datetime"))
            else:
                firstcreated = utcnow()

            text_nodes = article.xpath(".//text()")
            word_count = sum(
                len(text.strip().split()) for text in text_nodes if text.strip()
            )

            item = {
                ITEM_TYPE: CONTENT_TYPE.TEXT,
                GUID_FIELD: generate_guid(type=GUID_TAG),
                FORMAT: FORMATS.HTML,
                "headline": self._get_text(title),
                "abstract": self._get_text(abstract),
                "versioncreated": firstcreated,
                "firstcreated": firstcreated,
                "byline": self._get_text(author),
                "body_html": to_string(article, method="html", remove_root_div=True),
                "word_count": word_count,
                "source": "Medium",
            }

            self.parse_images(item, article)

            # try to extract a slugline from the title
            title_text = self._get_text(title)
            if ":" in title_text:
                item["slugline"] = title_text.split(":")[0].strip()

            # keep original article url
            canonical = root.find(".//a[@class='p-canonical']")
            if canonical is not None and canonical.get("href"):
                if "extra" not in item:
                    item["extra"] = {}
                item["extra"]["original_article_url"] = canonical.get("href")

            return item
        except Exception as ex:
            raise ParserError.parseFileError(file_path, ex)


register_feed_parser(MediumParser.NAME, MediumParser())
