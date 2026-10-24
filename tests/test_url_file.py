import json
import tempfile
import unittest
from pathlib import Path

from product_crawler.crawl import CrawlerError
from product_crawler.url_file import UrlFileError, run_file


class UrlFileTests(unittest.TestCase):
    def test_text_file_writes_uuid_beside_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "urls.txt"
            path.write_text(
                "https://example.com/products/a\n"
                "https://example.com/products/done 11111111-1111-1111-1111-111111111111\n",
                encoding="utf-8",
            )
            seen: list[str] = []

            def fake_crawl(url: str) -> Path:
                seen.append(url)
                return Path(tmp) / "output" / "22222222-2222-2222-2222-222222222222"

            code = run_file(path, fake_crawl)
            self.assertEqual(code, 0)
            self.assertEqual(seen, ["https://example.com/products/a"])
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "https://example.com/products/a 22222222-2222-2222-2222-222222222222\n"
                "https://example.com/products/done 11111111-1111-1111-1111-111111111111\n",
            )

    def test_text_file_keeps_comments_and_saves_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "urls.txt"
            path.write_text(
                "# batch\n"
                "\n"
                "https://example.com/products/bad\n"
                "https://example.com/products/ok\n",
                encoding="utf-8",
            )

            def fake_crawl(url: str) -> Path:
                if url.endswith("/bad"):
                    raise CrawlerError("page missing")
                return Path(tmp) / "33333333-3333-3333-3333-333333333333"

            code = run_file(path, fake_crawl)
            self.assertEqual(code, 1)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# batch\n"
                "\n"
                "https://example.com/products/bad\n"
                "https://example.com/products/ok 33333333-3333-3333-3333-333333333333\n",
            )

    def test_json_file_records_folder_on_each_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "urls.json"
            path.write_text(
                json.dumps(
                    [
                        "https://example.com/products/a",
                        {
                            "url": "https://example.com/products/done",
                            "folder": "11111111-1111-1111-1111-111111111111",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            def fake_crawl(url: str) -> Path:
                self.assertEqual(url, "https://example.com/products/a")
                return Path("output") / "22222222-2222-2222-2222-222222222222"

            self.assertEqual(run_file(path, fake_crawl), 0)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved,
                [
                    {
                        "url": "https://example.com/products/a",
                        "folder": "22222222-2222-2222-2222-222222222222",
                    },
                    {
                        "url": "https://example.com/products/done",
                        "folder": "11111111-1111-1111-1111-111111111111",
                    },
                ],
            )

    def test_invalid_url_is_rejected_before_crawl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "urls.txt"
            path.write_text("ftp://example.com/products/shoe\n", encoding="utf-8")

            def fake_crawl(url: str) -> Path:
                raise AssertionError(url)

            with self.assertRaises(UrlFileError):
                run_file(path, fake_crawl)
            self.assertEqual(path.read_text(encoding="utf-8"), "ftp://example.com/products/shoe\n")


if __name__ == "__main__":
    unittest.main()
