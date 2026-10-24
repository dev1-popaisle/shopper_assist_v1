import unittest

from product_crawler.blocked import (
    blocked_note,
    page_matches,
    product_clues,
    rank_candidates,
    search_result_urls,
)

RAYBAN = (
    "https://www.ray-ban.com/usa/electronics/"
    "RW4011Vray-ban%20meta%20gen%203%20wayfarer%20optics-black/8056266264597"
)


class BlockedFetchTests(unittest.TestCase):
    def test_url_clues_keep_the_ean_and_style_code(self) -> None:
        self.assertEqual(product_clues(RAYBAN), ["8056266264597", "RW4011V"])

    def test_amazon_path_keeps_the_asin_and_ignores_ad_numbers(self) -> None:
        url = "https://www.amazon.com/Fenmzee-Small-Table-Lamp-Bedroom/dp/B0DZD1X83N/ref=sr_1_16?hvadid=815785477351"
        self.assertEqual(product_clues(url), ["B0DZD1X83N"])

    def test_search_results_skip_the_blocked_host_and_prefer_the_same_ean(self) -> None:
        page = """
        <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.ray-ban.com%2Fusa%2F8056266264597&amp;rut=1">same</a>
        <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fshop.example%2Fother%2F8056266191015&amp;rut=2">other code</a>
        <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fshop.example%2Fglasses%2F8056266264597&amp;rut=3">match</a>
        """
        urls = search_result_urls(page)
        ranked = rank_candidates(urls, ["8056266264597", "RW4011V"], RAYBAN)
        self.assertEqual(ranked[0], "https://shop.example/glasses/8056266264597")
        self.assertNotIn("https://www.ray-ban.com/usa/8056266264597", ranked)

    def test_another_color_does_not_match_this_ean(self) -> None:
        clues = ["8056266264597", "RW4011V"]
        self.assertFalse(page_matches("<html>RW4011V 8056266264573</html>", clues))
        self.assertTrue(page_matches("<html>style RW4011V ean 8056266264597</html>", clues))

    def test_amazon_regional_storefronts_are_the_same_blocked_site(self) -> None:
        ranked = rank_candidates(
            [
                "https://www.amazon.ca/dp/B0DZD1X83N",
                "https://www.amazon.co.za/dp/B0DZD1X83N",
                "https://www.justbid.com/item/OLARR8470237/7315365",
            ],
            ["B0DZD1X83N"],
            "https://www.amazon.com/dp/B0DZD1X83N",
        )
        self.assertEqual(ranked, ["https://www.justbid.com/item/OLARR8470237/7315365"])

    def test_blocked_note_names_both_urls(self) -> None:
        note = blocked_note("https://shop.example/original", "https://other.example/product")
        self.assertIn("urls.txt", note)
        self.assertIn("blocked the crawler", note)
        self.assertIn("https://shop.example/original", note)
        self.assertIn("https://other.example/product", note)


if __name__ == "__main__":
    unittest.main()
