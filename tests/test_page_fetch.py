import tempfile
import unittest
from pathlib import Path

from product_crawler.extract import assemble_product
from product_crawler.page_fetch import (
    archive_target,
    document_is_blocked,
    html_from_exa,
    recover_blocked_page,
    shell_title,
)

AMAZON = """
<html><head><title>Amazon.com</title></head><body>
  <a href="#skip">Skip to</a>
  <h1 id="productTitle">Fenmzee Small Table Lamp</h1>
  <a id="bylineInfo">Visit the Fenmzee Store</a>
  <input id="ASIN" value="B0DZD1X83N" />
  <img id="landingImage" data-old-hires="https://images.example/lamp.jpg" alt="lamp" />
  <ul id="feature-bullets"><li>3000K warm light with an inline switch</li></ul>
  <div>UPC: 012345678905</div>
</body></html>
"""


class PageFetchTests(unittest.TestCase):
    def test_amazon_block_screen_is_rejected(self) -> None:
        html = "<html><title>Amazon.com</title><body>Click the button below to continue shopping</body></html>"
        self.assertTrue(document_is_blocked(html))
        self.assertTrue(shell_title("Skip to"))

    def test_amazon_path_drops_the_ad_query_for_a_cache_lookup(self) -> None:
        url = "https://www.amazon.com/Fenmzee-Small-Table-Lamp-Bedroom/dp/B0DZD1X83N/ref=sr_1_16?hvadid=815785477351"
        self.assertEqual(
            archive_target(url),
            "https://www.amazon.com/Fenmzee-Small-Table-Lamp-Bedroom/dp/B0DZD1X83N/ref=sr_1_16",
        )

    def test_local_copy_is_scraped_and_a_cache_names_the_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []

            def blocked_live() -> None:
                calls.append("live")
                return None

            def archive() -> tuple[str, str, str]:
                calls.append("archive")
                return AMAZON, "https://web.archive.org/web/20260101id_/https://www.amazon.com/dp/B0DZD1X83N", "wayback"

            recovered = recover_blocked_page(
                "https://www.amazon.com/dp/B0DZD1X83N",
                client=None,
                cache_dir=Path(tmp),
                fetchers=[blocked_live, archive],
            )

        self.assertEqual(calls, ["live", "archive"])
        assert recovered is not None
        html, source, note, method = recovered
        self.assertEqual(method, "wayback")
        self.assertIn("scraped locally", note or "")
        self.assertIn("web.archive.org", source)
        record = assemble_product(
            "https://www.amazon.com/dp/B0DZD1X83N",
            html,
            None,
            None,
            product_js_url=None,
            reviews_endpoint=None,
        )
        self.assertEqual(record["title"], "Fenmzee Small Table Lamp")
        self.assertEqual(record["brand"], "Fenmzee")
        self.assertEqual(record["product_id"], "B0DZD1X83N")
        self.assertEqual(record["barcode"], "012345678905")
        self.assertEqual(record["images"][0]["src"], "https://images.example/lamp.jpg")
        self.assertIn("3000K warm light with an inline switch", record["features"])

    def test_a_live_browser_fetch_keeps_the_original_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovered = recover_blocked_page(
                "https://www.amazon.com/dp/B0DZD1X83N",
                client=None,
                cache_dir=Path(tmp),
                fetchers=[lambda: (AMAZON, "https://www.amazon.com/dp/B0DZD1X83N", "curl_cffi")],
            )
            saved = list(Path(tmp).glob("*.html"))
        assert recovered is not None
        _html, source, note, method = recovered
        self.assertEqual(method, "curl_cffi")
        self.assertIsNone(note)
        self.assertEqual(source, "https://www.amazon.com/dp/B0DZD1X83N")
        self.assertEqual(len(saved), 1)

    def test_exa_text_is_scraped_locally_and_names_a_different_page(self) -> None:
        page = html_from_exa(
            {
                "title": "Fenmzee Small Table Lamp",
                "url": "https://shop.example/lamp",
                "text": "Cream shade wood base bedside lamp with an inline switch.",
                "image": "https://images.example/lamp.jpg",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            recovered = recover_blocked_page(
                "https://www.amazon.com/dp/B0DZD1X83N",
                client=None,
                cache_dir=Path(tmp),
                fetchers=[lambda: (page, "https://shop.example/lamp", "exa")],
            )
        assert recovered is not None
        html, source, note, method = recovered
        self.assertEqual(method, "exa")
        self.assertEqual(source, "https://shop.example/lamp")
        self.assertIn("blocked the crawler", note or "")
        record = assemble_product(
            "https://www.amazon.com/dp/B0DZD1X83N",
            html,
            None,
            None,
            product_js_url=None,
            reviews_endpoint=None,
        )
        self.assertEqual(record["title"], "Fenmzee Small Table Lamp")
        self.assertEqual(record["images"][0]["src"], "https://images.example/lamp.jpg")


if __name__ == "__main__":
    unittest.main()
