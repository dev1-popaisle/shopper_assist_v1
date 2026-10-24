import unittest

from product_crawler.openai_enrich import _source_urls
from product_crawler.prompt_search import page_matches_product, photo_urls, printed_barcode

PAGE = """
<html><head>
<title>JBL Vibe 200TWS True Wireless Earbuds, Black - Office Depot</title>
</head><body>
<script type="application/ld+json">
{"@type":"Product","name":"JBL Vibe 200TWS","image":"https://media.example/products/7822239/7822239_o01.jpg"}
</script>
<p>UPC: 050036382984</p>
<img src="https://media.example/brand.png">
</body></html>
"""


class PromptPageTests(unittest.TestCase):
    def test_page_supplies_the_printed_barcode_and_product_photo(self) -> None:
        record = {"title": "JBL Vibe 200TWS True Wireless Earbuds", "brand": "JBL"}
        self.assertTrue(page_matches_product(PAGE, "https://www.officedepot.com/a/products/7822239/", record))
        self.assertEqual(printed_barcode(PAGE), "050036382984")
        self.assertEqual(
            photo_urls(PAGE, "https://www.officedepot.com/a/products/7822239/"),
            ["https://media.example/products/7822239/7822239_o01.jpg"],
        )

    def test_a_later_generation_page_is_not_the_same_product(self) -> None:
        html = "<html><head><title>AirPods Pro 3 - Apple</title></head><body>Apple</body></html>"
        record = {"title": "AirPods Pro 2 with MagSafe Charging Case (USB-C)", "brand": "Apple"}
        self.assertFalse(page_matches_product(html, "https://www.apple.com/airpods-pro/", record))

    def test_source_urls_drop_the_search_referral(self) -> None:
        payload = {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [{"url": "https://www.jbl.com/earbuds/VIBE200TWS.html?utm_source=openai"}]
                    },
                }
            ]
        }
        self.assertEqual(_source_urls(payload), ["https://www.jbl.com/earbuds/VIBE200TWS.html"])


if __name__ == "__main__":
    unittest.main()
