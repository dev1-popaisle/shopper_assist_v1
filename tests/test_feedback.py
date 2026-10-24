import unittest

from product_crawler.extract import assemble_product, latest_feedbacks
from product_crawler.feedback import feedbacks_from_html, parse_review_feed, review_feed_urls


class FeedbackTests(unittest.TestCase):
    def test_page_feedback_blocks_keep_the_ten_newest(self) -> None:
        blocks = []
        for index in range(12):
            day = index + 1
            words = " ".join(f"word{n}" for n in range(130))
            blocks.append(
                f"""
                <article class="customer-feedback">
                  <span class="author">Person {index}</span>
                  <time datetime="2026-02-{day:02d}">February {day}</time>
                  <p class="feedback-body">{words}</p>
                </article>
                """
            )
        html = "<section class='customer-feedbacks'>" + "".join(blocks) + "</section>"
        selected = latest_feedbacks(feedbacks_from_html(html))
        self.assertEqual(len(selected), 10)
        self.assertEqual(selected[0]["nickname"], "Person 11")
        self.assertEqual(len(selected[0]["comments"].split()), 100)
        self.assertEqual(selected[0]["source"], "page")

    def test_jsonld_reviews_are_sorted_with_page_feedback(self) -> None:
        html = """
        <script type="application/ld+json">
        {"@type":"Product","review":[
          {"@type":"Review","author":{"@type":"Person","name":"Ada"},
           "datePublished":"2026-01-01","reviewBody":"Ada liked the cushion on the daily trainer a lot",
           "name":"Solid","reviewRating":{"ratingValue":"4"}},
          {"@type":"Review","author":"Bea","datePublished":"2026-03-01",
           "reviewBody":"Bea said the newer pair feels lighter than the old pair","name":"Lighter"}
        ]}
        </script>
        <div class="user-feedback">
          <span class="author">Cleo</span>
          <time datetime="2026-02-01"></time>
          <div class="feedback-body">Cleo wrote that the fit stayed comfortable after a long walk</div>
        </div>
        """
        selected = latest_feedbacks(feedbacks_from_html(html))
        self.assertEqual([item["nickname"] for item in selected], ["Bea", "Cleo", "Ada"])

    def test_feed_payload_and_widget_url(self) -> None:
        html = """
        <link rel="canonical" href="https://shop.example/products/shoe">
        <div class="yotpo" data-product-id="99" data-appkey="abc"></div>
        """
        feeds = review_feed_urls(html)
        self.assertEqual(feeds[0][0], "yotpo")
        self.assertIn("/products/99/reviews.json", feeds[0][1])
        parsed = parse_review_feed(
            {
                "response": {
                    "reviews": [
                        {
                            "id": 7,
                            "score": 5,
                            "title": "Again",
                            "content": "I would buy this shoe again for easy miles",
                            "user": {"display_name": "Nina"},
                            "created_at": "2026-04-02T00:00:00+00:00",
                        }
                    ]
                }
            }
        )
        self.assertEqual(parsed[0]["nickname"], "Nina")
        self.assertEqual(parsed[0]["rating"], 5)
        self.assertIn("easy miles", parsed[0]["comments"])

    def test_unmarked_feedback_section_is_still_read(self) -> None:
        html = """
        <h2>Customer feedback</h2>
        <div>
          <div>
            <span class="author">Sam</span>
            <time datetime="2026-06-02"></time>
            The cushion stayed soft through a long easy run in the park
          </div>
          <div>
            <span class="author">Rio</span>
            <time datetime="2026-06-01"></time>
            The toe box was roomy enough for a wide foot on pavement
          </div>
        </div>
        <div class="reviews-list">
          <p class="item">A third note about grip on wet roads during the morning commute</p>
          <p class="item">A fourth note about the laces staying tied over ten miles</p>
        </div>
        <div id="description">
          <p>This trainer is built for daily miles with a soft foam midsole and a light mesh upper.</p>
          <p>The outsole uses rubber only where the foot lands so the shoe stays lighter.</p>
        </div>
        """
        selected = latest_feedbacks(feedbacks_from_html(html))
        self.assertEqual(
            [item["nickname"] for item in selected if item["nickname"]],
            ["Sam", "Rio"],
        )
        self.assertEqual(len(selected), 4)
        self.assertTrue(all("midsole" not in item["comments"] for item in selected))

    def test_assemble_merges_widget_and_page_feedback(self) -> None:
        html = """
        <html><body>
          <h1>Trail Shoe</h1>
          <article class="review">
            <span class="author">Page Person</span>
            <time datetime="2026-08-01"></time>
            <div class="review-body">The page feedback says the outsole gripped wet roads well</div>
          </article>
        </body></html>
        """
        record = assemble_product(
            "https://example.com/products/shoe",
            html,
            {"title": "Trail Shoe", "id": 1},
            {
                "reviews": [
                    {
                        "review_id": 1,
                        "details": {
                            "headline": "Older",
                            "comments": "The widget feedback is from an earlier month than the page",
                            "nickname": "Widget Person",
                            "created_date": 1767225600000,
                        },
                        "metrics": {"rating": 4},
                        "badges": {},
                    }
                ]
            },
            product_js_url=None,
            reviews_endpoint=None,
        )
        self.assertEqual(record["reviews"][0]["nickname"], "Page Person")
        self.assertEqual(record["reviews"][1]["nickname"], "Widget Person")
        self.assertIsNone(record["reviews_note"])

    def test_missing_feedback_is_recorded_in_the_product(self) -> None:
        record = assemble_product(
            "https://example.com/products/plain",
            "<html><body><h1>Plain Shoe</h1><p>A daily trainer.</p></body></html>",
            {"title": "Plain Shoe", "id": 2},
            None,
            product_js_url=None,
            reviews_endpoint=None,
        )
        self.assertEqual(record["reviews"], [])
        self.assertEqual(record["reviews_note"], "No user feedback found on this page.")

    def test_amazon_page_chrome_is_not_a_review(self) -> None:
        html = """
        <div class="review">
          <p>Where did you see a lower price? Please sign in to provide feedback.</p>
        </div>
        <div class="review">
          <p>5 star 4 star 3 star 2 star 1 star 79% 10% 5% 2% 4%</p>
        </div>
        <div class="customer-review">
          <p>The inline switch is easy to reach and the warm bulb is bright enough to read by at night.</p>
        </div>
        """
        comments = [item["comments"] for item in feedbacks_from_html(html)]
        self.assertEqual(len(comments), 1)
        self.assertIn("inline switch", comments[0])


if __name__ == "__main__":
    unittest.main()
