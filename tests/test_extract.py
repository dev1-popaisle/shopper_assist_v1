import unittest

from product_crawler.extract import (
    assemble_product,
    cents_to_price,
    collect_images,
    image_bytes_ok,
    select_variant,
    shopify_js_url,
)

PAGE = """
<html><head>
  <meta property="og:title" content="Men's Clifton 11" />
  <meta property="og:price:amount" content="155.00" />
  <meta property="og:price:currency" content="USD" />
</head><body>
  <div id="product-overview">
    <div class="breadcrumbs">
      <div class="crumb"><a href="/search?q=Running">Running</a></div>
      <div class="crumb"><a href="/search?q=Footwear">Footwear</a></div>
    </div>
    <div class="product-flags"></div>
    <a href="/collections/hoka">Hoka</a>
  </div>
  <ul>
    <li>
      <svg><use href="#weight"></use></svg>
      <span class="font-bold">WEIGHT: </span>
      <span>9.97 oz (Per Shoe)</span>
    </li>
    <li>
      <svg><use href="#drop"></use></svg>
      <span class="font-bold">DROP: </span>
      <span>8 mm</span>
    </li>
    <li>
      <svg><use href="#widths"></use></svg>
      <span class="font-bold">WIDTHS: </span>
      <span>Regular, Wide &amp; Extra Wide</span>
    </li>
  </ul>
  <script>
    POWERREVIEWS.display.render({
      api_key: 'test-key',
      locale: 'en_US',
      merchant_group_id: '1',
      merchant_id: '965487',
      page_id: '7014038175816',
      review_wrapper_url: 'https://example.com/review',
      product: { name: 'shoe' }
    });
  </script>
</body></html>
"""

DESCRIPTION = """
<p>A cushioned trainer.</p>
<ul>
  <li>Engineered mesh upper</li>
  <li>Abrasion rubber outsole</li>
  <li>Compression molded EVA foam midsole</li>
  <li>Weight 9.97 oz</li>
  <li>Heel-To-Toe Drop: 8.00 mm</li>
  <li>Stability: Neutral</li>
  <li>Cushion: Balanced</li>
  <li>Smooth MetaRocker</li>
</ul>
"""


def _product() -> dict:
    return {
        "id": 7014038175816,
        "title": "Men's Clifton 11",
        "handle": "clifton-16",
        "vendor": "Hoka",
        "type": "LDAD01",
        "description": DESCRIPTION,
        "tags": [
            "Basement10",
            "Best Use:Road",
            "Drop:5-10 mm",
            "Shoe Width:D (Men's Medium)",
            "Weight (Per Shoe):9-9.9 oz",
            "BreadcrumbActivity:Running",
        ],
        "price": 15500,
        "available": True,
        "options": [
            {"name": "Color", "position": 1, "values": ["BWHT-Black/White"]},
            {"name": "Size", "position": 2, "values": ["7", "8"]},
        ],
        "variants": [
            {
                "id": 111,
                "title": "BWHT-Black/White / 8",
                "sku": "other",
                "price": 15500,
                "compare_at_price": 15500,
                "available": True,
                "options": ["BWHT-Black/White", "8"],
                "barcode": "000",
            },
            {
                "id": 41172026130504,
                "title": "BWHT-Black/White / 7",
                "name": "Men's Clifton 11 - BWHT-Black/White / 7",
                "sku": "2050014189039",
                "barcode": "100301256016",
                "price": 15500,
                "compare_at_price": 15500,
                "available": True,
                "inventory_quantity": 13,
                "options": ["BWHT-Black/White", "7"],
                "featured_image": {
                    "src": "https://cdn.shopify.com/s/files/1/x/black.png?v=1",
                },
            },
        ],
        "media": [
            {
                "media_type": "image",
                "position": 1,
                "alt": "side",
                "width": 1200,
                "height": 1200,
                "src": "https://cdn.shopify.com/s/files/1/x/black.png?v=1",
            },
            {
                "media_type": "video",
                "position": 2,
                "src": "https://cdn.shopify.com/s/files/1/x/clip.mp4",
            },
            {
                "media_type": "image",
                "position": 3,
                "alt": "sole",
                "src": "https://cdn.shopify.com/s/files/1/x/sole.webp",
            },
        ],
    }


class ExtractTests(unittest.TestCase):
    def test_cents_and_variant_selection(self) -> None:
        self.assertEqual(cents_to_price(15500), "155.00")
        variant, how = select_variant(_product(), "41172026130504")
        self.assertEqual(how, "query_param")
        assert variant is not None
        self.assertEqual(variant["sku"], "2050014189039")
        missing, missing_how = select_variant(_product(), "999")
        self.assertIsNone(missing)
        self.assertEqual(missing_how, "query_param_not_found")

    def test_product_group_supplies_name_brand_image_and_barcode(self) -> None:
        html = """
        <html><body>
          <h2>New Arrivals</h2>
          <script type="application/ld+json">
          {"@type":"ProductGroup","name":"Nike Vomero 18 - Men's","sku":"M6803101",
           "brand":{"@type":"Brand","name":"Nike"},
           "image":"https://assets.example.com/is/image/FLDM/M6803101_01",
           "offers":{"price":"155","priceCurrency":"USD"},
           "hasVariant":[{"sku":"HQ2050-003","gtin":"00198966992726","name":"Vomero HQ2050-003"}]}
          </script>
        </body></html>
        """
        record = assemble_product(
            "https://example.com/t/vomero/HQ2050-003",
            html,
            None,
            None,
            product_js_url=None,
            reviews_endpoint=None,
        )
        self.assertEqual(record["title"], "Nike Vomero 18 - Men's")
        self.assertEqual(record["brand"], "Nike")
        self.assertEqual(record["barcode"], "00198966992726")
        self.assertEqual(record["price"], "155")
        self.assertEqual(record["images"][0]["src"], "https://assets.example.com/is/image/FLDM/M6803101_01")

    def test_shopify_endpoint_strips_query(self) -> None:
        url = shopify_js_url(
            "https://www.sportsbasement.com/products/clifton-16?variant=41172026130504"
        )
        self.assertEqual(url, "https://www.sportsbasement.com/products/clifton-16.js")

    def test_assemble_honors_variant_and_page_fields(self) -> None:
        page = "https://www.sportsbasement.com/products/clifton-16?variant=41172026130504"
        record = assemble_product(
            page,
            PAGE,
            _product(),
            {
                "rollup": {
                    "average_rating": 2.84,
                    "rating_count": 67,
                    "review_count": 62,
                    "recommended_ratio": 0.4,
                    "rating_histogram": [22, 13, 5, 8, 19],
                },
                "reviews": [
                    {
                        "review_id": 1,
                        "details": {
                            "headline": "Would buy again",
                            "comments": "Better than the Clifton 10s.",
                            "nickname": "A",
                            "product_page_id": "7014038175816",
                            "created_date": 1789005867725,
                        },
                        "metrics": {"rating": 5, "helpful_votes": 1, "not_helpful_votes": 0},
                        "badges": {"is_verified_buyer": True, "is_verified_reviewer": False},
                    }
                ],
            },
            product_js_url="https://www.sportsbasement.com/products/clifton-16.js",
            reviews_endpoint="https://display.powerreviews.com/m/965487/l/en_US/product/7014038175816/reviews",
        )
        self.assertEqual(record["title"], "Men's Clifton 11")
        self.assertEqual(record["brand"], "Hoka")
        self.assertEqual(record["price"], "155.00")
        self.assertEqual(record["currency"], "USD")
        self.assertEqual(record["sku"], "2050014189039")
        self.assertEqual(record["availability"], "in_stock")
        self.assertEqual(record["selected_variant"]["id"], 41172026130504)
        self.assertEqual(record["selected_variant"]["options"]["Size"], "7")
        self.assertEqual(record["selected_variant"]["options"]["Color"], "BWHT-Black/White")
        self.assertEqual(record["dimensions"]["heel_to_toe_drop"], "8.00 mm")
        self.assertIsNone(record["dimensions"]["length"])
        self.assertEqual(record["dimensions"]["widths_available"], "Regular, Wide & Extra Wide")
        self.assertEqual(record["weight"]["value"], 9.97)
        self.assertEqual(record["weight"]["unit"], "oz")
        self.assertIn("Engineered mesh upper", record["materials"])
        self.assertIn("Abrasion rubber outsole", record["materials"])
        self.assertEqual(record["specs"]["drop"], "8 mm")
        self.assertEqual(record["specs"]["drop_range"], "5-10 mm")
        self.assertEqual(record["ranking"]["designation"], "Basement10")
        self.assertIsNone(record["ranking"]["numeric_rank"])
        self.assertIsNone(record["popularity"])
        self.assertEqual(record["star_rating"], 2.84)
        self.assertEqual(record["review_count"], 62)
        self.assertEqual(record["reviews"][0]["headline"], "Would buy again")
        self.assertEqual(len(record["reviews"][0]["comments"].split()), 5)
        self.assertEqual([crumb["name"] for crumb in record["breadcrumbs"]], ["Running", "Footwear"])
        self.assertEqual([image["src"].endswith("clip.mp4") for image in record["images"]], [False, False])
        self.assertEqual(len(record["images"]), 2)

    def test_feedbacks_are_the_ten_newest_and_capped_at_100_words(self) -> None:
        from product_crawler.extract import latest_feedbacks

        reviews = []
        for index in range(12):
            reviews.append(
                {
                    "id": index,
                    "created_at": f"2026-01-{index + 1:02d}T00:00:00+00:00",
                    "headline": f"Review {index}",
                    "comments": " ".join(f"word{n}" for n in range(140)),
                }
            )
        selected = latest_feedbacks(list(reversed(reviews)))
        self.assertEqual(len(selected), 10)
        self.assertEqual([item["id"] for item in selected], list(range(11, 1, -1)))
        self.assertEqual(len(selected[0]["comments"].split()), 100)
        self.assertTrue(selected[0]["comments"].startswith("word0 "))

    def test_video_bytes_are_rejected(self) -> None:
        self.assertFalse(image_bytes_ok(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40, "video/mp4"))
        self.assertTrue(image_bytes_ok(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png"))

    def test_collect_images_skips_video(self) -> None:
        from bs4 import BeautifulSoup

        images = collect_images(_product(), BeautifulSoup("<html></html>", "html.parser"), "https://example.com/p")
        names = [image["src"].rsplit("/", 1)[-1].split("?", 1)[0] for image in images]
        self.assertEqual(names, ["black.png", "sole.webp"])


if __name__ == "__main__":
    unittest.main()
