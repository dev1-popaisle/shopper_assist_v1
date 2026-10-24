import unittest

from product_crawler.openai_enrich import (
    _prompt,
    apply_findings,
    apply_prompt_findings,
    merge_offsite,
    missing_required,
    product_videos,
    _embedded_page_videos,
    unfilled,
)


class OpenAIEnrichTests(unittest.TestCase):
    def test_embedded_page_video_is_found(self) -> None:
        videos = _embedded_page_videos(
            '<html><iframe src="https://www.youtube.com/embed/lamp123" title="Lamp setup"></iframe></html>',
            "https://shop.example/lamp",
        )
        self.assertEqual(videos[0]["url"], "https://www.youtube.com/embed/lamp123")
    def test_prompt_search_builds_a_record_without_a_crawled_url(self) -> None:
        record, images = apply_prompt_findings(
            "small cream table lamp",
            {
                "product_name": "Fenmzee Small Table Lamp",
                "brand": "Fenmzee",
                "barcode": "012345678905",
                "barcode_note": "single lamp",
                "product_page_url": "https://shop.example/lamp",
                "price": "19.99",
                "currency": "USD",
                "description": "A small cream bedside lamp.",
                "features": ["Inline switch", "3000K bulb"],
                "image_url": "https://images.example/lamp.jpg",
                "feedbacks": [
                    {"nickname": "Ada", "comments": "The inline switch is easy to reach from the bed."}
                ],
                "videos": [{"url": "https://www.youtube.com/watch?v=lamp123", "title": "Lamp setup", "category": "how to use"}],
                "research": {"average_price": "$20"},
                "sources": ["https://example.com/lamp"],
            },
        )
        self.assertEqual(record["origin"], "openai_prompt")
        self.assertEqual(record["requested_url"], "https://shop.example/lamp")
        self.assertEqual(record["sources"]["product_page"], "https://shop.example/lamp")
        self.assertEqual(record["title"], "Fenmzee Small Table Lamp")
        self.assertEqual(record["brand"], "Fenmzee")
        self.assertEqual(record["barcode"], "012345678905")
        self.assertEqual(record["price"], "19.99")
        self.assertEqual(record["currency"], "USD")
        self.assertEqual(record["description"]["text"], "A small cream bedside lamp.")
        self.assertEqual(record["features"], ["Inline switch", "3000K bulb"])
        self.assertIn("variants", record)
        self.assertEqual(images[0]["src"], "https://images.example/lamp.jpg")
        self.assertEqual(record["videos"][0]["url"], "https://www.youtube.com/watch?v=lamp123")

    def test_complete_record_is_not_sent_for_more_search(self) -> None:
        record = {
            "title": "Trail Shoe",
            "brand": "Hoka",
            "barcode": "100301256016",
            "reviews": [{"comments": "A long enough buyer comment about the shoe"}],
        }
        self.assertEqual(missing_required(record, has_image=True), [])

    def test_findings_fill_only_the_gaps_and_keep_research(self) -> None:
        record = {"title": "Trail Shoe", "brand": "Hoka", "barcode": None, "reviews": []}
        images = apply_findings(
            record,
            {
                "brand": "Other",
                "barcode": "00198966992726",
                "product_name": "Different name",
                "image_url": "https://cdn.example.com/shoe.jpg",
                "feedbacks": [
                    {
                        "nickname": "Ada",
                        "created_at": "2026-03-01",
                        "rating": 5,
                        "comments": "Ada liked the cushion on the daily trainer a lot",
                    }
                ],
                "research": {
                    "audience_gender": "Men",
                    "popularity": "Popular in the US",
                    "age_group": "Adults",
                    "professionals": "Runners",
                    "demographic_appeal": "Road runners",
                    "top_us_state": "Unknown",
                    "top_us_city": "Unknown",
                    "health_benefits": "Unknown",
                    "average_price": "$155",
                    "bundle": "Running socks",
                    "recalls": "None found",
                },
                "sources": ["https://example.com/shoe"],
            },
            ["barcode", "product image", "user feedback"],
        )
        self.assertEqual(record["brand"], "Hoka")
        self.assertEqual(record["title"], "Trail Shoe")
        self.assertEqual(record["barcode"], "00198966992726")
        self.assertEqual(images[0]["src"], "https://cdn.example.com/shoe.jpg")
        self.assertEqual(record["reviews"][0]["nickname"], "Ada")
        self.assertIsNone(record["reviews_note"])
        self.assertEqual(record["research"]["audience_gender"], "Men")
        self.assertEqual(record["research"]["sources"], ["https://example.com/shoe"])
        self.assertEqual(record["openai_enrichment"]["status"], "completed")

    def test_missing_barcode_search_is_not_limited_to_the_crawled_url(self) -> None:
        text = _prompt(
            {
                "requested_url": "https://shop.example/item",
                "title": "Vomero 18",
                "brand": "Nike",
                "product_id": "M6803101",
                "sku": "HM6803-101",
                "barcode": None,
            },
            ["barcode", "brand"],
        )
        self.assertIn("not limited to the source URL", text)
        self.assertIn("other retailers", text)
        self.assertIn("M6803101", text)
        self.assertIn("HM6803-101", text)
        self.assertIn("GTIN", text)
        self.assertIn("not a barcode", text)

    def test_sku_from_the_crawled_url_is_sent_to_the_web_search(self) -> None:
        text = _prompt(
            {
                "requested_url": "https://shop.example/pegasus?sku=25406088",
                "title": "Pegasus Plus",
                "brand": "Nike",
                "sku": None,
                "barcode": None,
            },
            ["barcode"],
        )
        self.assertIn("25406088", text)

    def test_product_image_url_is_sent_so_style_codes_in_it_can_be_searched(self) -> None:
        text = _prompt(
            {
                "requested_url": "https://www.hoka.com/clifton",
                "title": "Clifton 11",
                "brand": "HOKA",
                "barcode": None,
                "images": [{"src": "https://cdn.example.com/1176572-GLCT_1.png"}],
            },
            ["barcode"],
        )
        self.assertIn("1176572-GLCT_1.png", text)

    def test_style_code_is_not_a_barcode_and_a_wider_search_can_fill_it(self) -> None:
        first = {"barcode": "M6803101", "brand": "Nike", "sources": ["https://shop.example/item"]}
        self.assertEqual(unfilled(first, ["barcode", "brand"]), ["barcode"])
        merged = merge_offsite(
            first,
            {
                "barcode": None,
                "quote": 'barcode: "197860769496"',
                "barcode_note": "US size 8",
                "brand": "Other",
                "sources": ["https://other.example/upc"],
            },
            ["barcode"],
        )
        self.assertEqual(merged["barcode"], "197860769496")
        self.assertEqual(merged["barcode_note"], "US size 8")
        self.assertEqual(merged["brand"], "Nike")
        self.assertEqual(
            merged["sources"],
            ["https://shop.example/item", "https://other.example/upc"],
        )

    def test_barcode_note_is_stored_with_the_upc(self) -> None:
        record = {"title": "Vomero 18", "brand": "Nike", "barcode": None, "reviews": []}
        apply_findings(
            record,
            {
                "barcode": "197860769496",
                "barcode_note": "US size 8 of style HM6803-101",
                "research": {},
                "sources": [],
            },
            ["barcode"],
        )
        self.assertEqual(record["barcode"], "197860769496")
        self.assertEqual(record["barcode_note"], "US size 8 of style HM6803-101")
        self.assertTrue(record["barcode_from_openai"])

    def test_video_search_keeps_two_watch_pages_and_drops_the_rest(self) -> None:
        videos = product_videos(
            [
                {"url": "https://www.youtube.com/results?search_query=shoe", "title": "Search", "category": "how-to"},
                {
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "title": "How to run in this shoe",
                    "category": "how to use",
                },
                {
                    "url": "https://youtu.be/def456",
                    "title": "Cushion review",
                    "category": "visual feedback",
                },
                {
                    "url": "https://vimeo.com/789",
                    "title": "Extra",
                    "category": "health benefits",
                },
            ]
        )
        self.assertEqual(
            [video["url"] for video in videos],
            ["https://www.youtube.com/watch?v=abc123", "https://youtu.be/def456"],
        )
        self.assertEqual(videos[0]["category"], "how to use")
        self.assertEqual(videos[1]["category"], "visual feedback")


if __name__ == "__main__":
    unittest.main()
