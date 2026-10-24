"""One DuckDB row per scraped product.

The crawl still writes output/<uuid>/product.json first. That document is
then stored here. The page reads the row, not the file.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_BARCODE_TYPES = {
    8: "EAN-8",
    12: "UPC-A",
    13: "EAN-13",
    14: "GTIN-14",
}


def default_db_path() -> Path:
    override = os.environ.get("PRODUCT_CRAWLER_DB")
    if override:
        return Path(override)
    return Path("data") / "products.duckdb"


def default_output_root() -> Path:
    return Path(os.environ.get("PRODUCT_CRAWLER_OUTPUT", "output"))


def classify_barcode(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    if re.fullmatch(r"[\d\s-]+", text):
        digits = re.sub(r"\D", "", text)
        if digits:
            return digits, _BARCODE_TYPES.get(len(digits), "barcode")
    return text, "barcode"


def product_name(record: dict[str, Any]) -> str | None:
    title = record.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def product_brand(record: dict[str, Any]) -> str | None:
    brand = record.get("brand")
    if isinstance(brand, str) and brand.strip():
        return brand.strip().upper()
    return None


def product_barcode(record: dict[str, Any]) -> Any:
    selected = record.get("selected_variant")
    if isinstance(selected, dict) and selected.get("barcode"):
        return selected.get("barcode")
    return record.get("barcode")


def local_image_file(folder: Path, record: dict[str, Any]) -> str | None:
    manifest = _manifest(folder)
    selected = record.get("selected_variant")
    variant_src = selected.get("image") if isinstance(selected, dict) else None
    target = _file_stem(variant_src) if isinstance(variant_src, str) else ""
    if target:
        for item in manifest:
            source = item.get("source_url")
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            if _file_stem(source) == target or target in Path(filename).name:
                return filename
    for item in manifest:
        filename = item.get("filename")
        if isinstance(filename, str) and filename:
            return filename
    image_dir = folder / "images"
    if image_dir.is_dir():
        files = sorted(path for path in image_dir.iterdir() if path.is_file())
        if files:
            return f"images/{files[0].name}"
    return None


class ProductStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id VARCHAR PRIMARY KEY,
                    source_url VARCHAR,
                    name VARCHAR,
                    brand VARCHAR,
                    barcode VARCHAR,
                    barcode_type VARCHAR,
                    image_file VARCHAR,
                    payload JSON,
                    updated_at TIMESTAMP
                )
                """
            )
            columns = [row[0] for row in con.execute("DESCRIBE products").fetchall()]
            if "brand" not in columns:
                con.execute("ALTER TABLE products ADD COLUMN brand VARCHAR")
            self._backfill_brand(con)

    def import_folder(self, folder: Path) -> dict[str, Any]:
        record = json.loads((folder / "product.json").read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError(f"{folder / 'product.json'} is not a product object")
        product_id = str(record.get("run_id") or folder.name)
        if not UUID_RE.match(product_id):
            raise ValueError(f"{folder} has no UUID run id")
        record["run_id"] = product_id
        return self._upsert(product_id, record, local_image_file(folder, record))

    def import_missing(self, output_root: Path) -> int:
        """Insert output folders that are not already rows.

        Rows already in the table are left as they were last viewed or edited.
        """
        if not output_root.is_dir():
            return 0
        added = 0
        for folder in sorted(path for path in output_root.iterdir() if path.is_dir()):
            if not UUID_RE.match(folder.name):
                continue
            if not (folder / "product.json").is_file():
                continue
            if self.get(folder.name) is not None:
                continue
            self.import_folder(folder)
            added += 1
        return added

    def list_products(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id, source_url, name, brand, barcode, barcode_type, image_file, updated_at,
                       json_extract_string(payload, '$.origin'), payload::VARCHAR
                FROM products
                ORDER BY updated_at DESC, id
                """
            ).fetchall()
        items = []
        for row in rows:
            item = self._summary(row[:8])
            item["origin"] = row[8]
            payload = json.loads(row[9]) if row[9] else {}
            item["brightdata_enriched"] = isinstance(payload, dict) and isinstance(payload.get("brightdata_crawler"), dict)
            items.append(item)
        return items

    def get(self, product_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT id, source_url, name, brand, barcode, barcode_type, image_file,
                       payload::VARCHAR, updated_at
                FROM products
                WHERE id = ?
                """,
                [product_id],
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[7]) if row[7] else {}
        item = self._summary(row[:7] + (row[8],))
        item["payload"] = payload
        return item

    def create(self, record: dict[str, Any], image_file: str | None = None) -> dict[str, Any]:
        product_id = str(record.get("run_id") or "")
        if not UUID_RE.match(product_id):
            raise ValueError("payload.run_id must be the output folder UUID")
        if self.get(product_id) is not None:
            raise FileExistsError(product_id)
        return self._upsert(product_id, record, image_file)

    def update(
        self,
        product_id: str,
        *,
        name: str | None = None,
        brand: str | None = None,
        barcode: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get(product_id)
        if current is None:
            return None
        payload = dict(current["payload"])
        if name is not None:
            payload["title"] = name
        if brand is not None:
            payload["brand"] = product_brand({"brand": brand})
        if barcode is not None:
            code, _kind = classify_barcode(barcode)
            stored = code if code is not None else None
            payload["barcode"] = stored
            selected = payload.get("selected_variant")
            if isinstance(selected, dict):
                selected = dict(selected)
                selected["barcode"] = stored
                payload["selected_variant"] = selected
        return self._upsert(product_id, payload, current.get("image_file"))

    def update_payload(self, product_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Replace a product's JSON payload without changing its indexed fields."""
        self.init()
        with self._connect() as con:
            result = con.execute(
                "UPDATE products SET payload = ?::JSON, updated_at = now() WHERE id = ? RETURNING id",
                [json.dumps(payload, ensure_ascii=False), product_id],
            ).fetchone()
        return self.get(product_id) if result else None

    def delete(self, product_id: str) -> bool:
        with self._connect() as con:
            before = con.execute(
                "SELECT 1 FROM products WHERE id = ?",
                [product_id],
            ).fetchone()
            if before is None:
                return False
            con.execute("DELETE FROM products WHERE id = ?", [product_id])
        return True

    def _upsert(
        self,
        product_id: str,
        record: dict[str, Any],
        image_file: str | None,
    ) -> dict[str, Any]:
        code, kind = classify_barcode(product_barcode(record))
        source = record.get("requested_url")
        source_url = source.strip() if isinstance(source, str) and source.strip() else None
        self.init()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO products (
                    id, source_url, name, brand, barcode, barcode_type, image_file, payload, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?::JSON, now())
                ON CONFLICT (id) DO UPDATE SET
                    source_url = excluded.source_url,
                    name = excluded.name,
                    brand = excluded.brand,
                    barcode = excluded.barcode,
                    barcode_type = excluded.barcode_type,
                    image_file = excluded.image_file,
                    payload = excluded.payload,
                    updated_at = now()
                """,
                [
                    product_id,
                    source_url,
                    product_name(record),
                    product_brand(record),
                    code,
                    kind,
                    image_file,
                    json.dumps(record, ensure_ascii=False),
                ],
            )
        stored = self.get(product_id)
        if stored is None:
            raise RuntimeError(f"product {product_id} was not stored")
        return stored

    def _connect(self):
        import duckdb

        return duckdb.connect(str(self.db_path))

    @staticmethod
    def _summary(row: tuple) -> dict[str, Any]:
        updated = row[7]
        return {
            "id": row[0],
            "source_url": row[1],
            "name": row[2],
            "brand": row[3],
            "barcode": row[4],
            "barcode_type": row[5],
            "image_file": row[6],
            "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
        }

    @staticmethod
    def _backfill_brand(con) -> None:
        rows = con.execute(
            "SELECT id, payload::VARCHAR FROM products WHERE brand IS NULL"
        ).fetchall()
        for product_id, payload in rows:
            record = json.loads(payload) if payload else {}
            if not isinstance(record, dict):
                continue
            brand = product_brand(record)
            if brand:
                con.execute(
                    "UPDATE products SET brand = ? WHERE id = ?",
                    [brand, product_id],
                )


def _manifest(folder: Path) -> list[dict[str, Any]]:
    path = folder / "image-manifest.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _file_stem(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    name = Path(urlparse(url).path).name
    return name.lower()


def publish_output(folder: Path) -> None:
    """Store a finished scrape when product.json is present."""
    folder = Path(folder)
    if not (folder / "product.json").is_file():
        return
    ProductStore(default_db_path()).import_folder(folder)
