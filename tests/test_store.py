import json
import tempfile
import unittest
from pathlib import Path

from product_crawler.store import ProductStore, classify_barcode


RUN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _folder(root: Path, barcode: str = "100301256016") -> Path:
    folder = root / RUN_ID
    folder.mkdir(parents=True)
    (folder / "images").mkdir()
    (folder / "images" / "01-shoe.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    record = {
        "requested_url": "https://example.com/products/shoe",
        "title": "Trail Shoe",
        "brand": "Hoka",
        "barcode": "ignored-when-variant-present",
        "run_id": RUN_ID,
        "selected_variant": {
            "barcode": barcode,
            "image": "https://cdn.example.com/shoe.png?v=1",
        },
        "features": ["cushion"],
    }
    (folder / "product.json").write_text(json.dumps(record), encoding="utf-8")
    (folder / "image-manifest.json").write_text(
        json.dumps(
            [
                {
                    "filename": "images/02-other.png",
                    "source_url": "https://cdn.example.com/other.png",
                },
                {
                    "filename": "images/01-shoe.png",
                    "source_url": "https://cdn.example.com/shoe.png?v=1",
                },
            ]
        ),
        encoding="utf-8",
    )
    return folder


class StoreTests(unittest.TestCase):
    def test_barcode_types(self) -> None:
        self.assertEqual(classify_barcode("100301256016"), ("100301256016", "UPC-A"))
        self.assertEqual(classify_barcode("0123456789012"), ("0123456789012", "EAN-13"))
        self.assertEqual(classify_barcode("01234567890123"), ("01234567890123", "GTIN-14"))
        self.assertEqual(classify_barcode("12345678"), ("12345678", "EAN-8"))

    def test_import_stores_one_row_and_skips_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = _folder(root / "output")
            store = ProductStore(root / "products.duckdb")
            stored = store.import_folder(folder)
            self.assertEqual(stored["name"], "Trail Shoe")
            self.assertEqual(stored["brand"], "HOKA")
            self.assertEqual(stored["barcode"], "100301256016")
            self.assertEqual(stored["barcode_type"], "UPC-A")
            self.assertEqual(stored["image_file"], "images/01-shoe.png")
            self.assertEqual(stored["payload"]["features"], ["cushion"])
            self.assertEqual(store.import_missing(root / "output"), 0)

            updated = store.update(RUN_ID, name="Renamed Shoe", barcode="0123456789012")
            assert updated is not None
            self.assertEqual(updated["name"], "Renamed Shoe")
            self.assertEqual(updated["barcode_type"], "EAN-13")
            self.assertEqual(updated["payload"]["title"], "Renamed Shoe")
            self.assertEqual(updated["payload"]["selected_variant"]["barcode"], "0123456789012")
            self.assertTrue(store.delete(RUN_ID))
            self.assertIsNone(store.get(RUN_ID))
            self.assertEqual(store.import_missing(root / "output"), 1)
            self.assertEqual(store.get(RUN_ID)["name"], "Trail Shoe")


if __name__ == "__main__":
    unittest.main()
