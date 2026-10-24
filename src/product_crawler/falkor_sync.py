"""Idempotent sync of local product records to the configured FalkorDB graph."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from falkordb import FalkorDB

from product_crawler.store import ProductStore, default_db_path, default_output_root

LOGGER = logging.getLogger(__name__)
GRAPH_NAME = "product_search"


def _falkor_settings() -> tuple[str, str]:
    candidates = (Path("configs/falkorDB.rtf"), Path(__file__).resolve().parents[2] / "configs" / "falkorDB.rtf")
    config = next((path for path in candidates if path.is_file()), None)
    if config is None:
        raise RuntimeError("configs/falkorDB.rtf was not found")
    text = config.read_text(encoding="utf-8", errors="replace")
    text = re.sub(
        r"\\'[0-9a-fA-F]{2}",
        lambda match: bytes([int(match.group(0)[2:], 16)]).decode("cp1252"),
        text,
    )
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text).replace("{", "").replace("}", "")
    settings = {
        key.strip().casefold(): value.strip()
        for key, value in re.findall(r"(?im)^\s*([^:\r\n]+?)\s*:\s*([^\\\r\n]+)", text)
    }
    url = settings.get("url flaker db")
    password = settings.get("user password")
    if not url or not password:
        raise RuntimeError("FalkorDB URL or password is missing from configs/falkorDB.rtf")
    return url, password


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _plain_text(item)))
    if isinstance(value, dict):
        return " ".join(part for item in value.values() if (part := _plain_text(item)))
    return ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _review_id(product_id: str, review: dict[str, Any]) -> str:
    content = _json({
        "nickname": review.get("nickname") or review.get("author"),
        "created_at": review.get("created_at") or review.get("date"),
        "rating": review.get("rating"),
        "comments": review.get("comments") or review.get("text") or review.get("content"),
    })
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
    return f"{product_id}:{digest}"


def _clean_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _plain_text(item).strip()
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _row_for_graph(row: dict[str, Any], output_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    product_id = str(row["id"])
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    description = _plain_text(payload.get("description"))
    features = payload.get("features") or payload.get("description_points") or []
    specs = payload.get("specs") or []
    tags = payload.get("tags") or []
    reviews = payload.get("reviews") if isinstance(payload.get("reviews"), list) else []
    research = payload.get("research") if isinstance(payload.get("research"), dict) else {}
    search_text = " ".join(
        part for part in (
            str(row.get("name") or ""),
            str(row.get("brand") or ""),
            str(row.get("source_url") or ""),
            description,
            _plain_text(features),
            _plain_text(specs),
            _plain_text(tags),
            _plain_text(reviews),
            _plain_text(research),
        ) if part
    )

    folder = output_root / product_id
    manifest_file = folder / "image-manifest.json"
    try:
        image_manifest = manifest_file.read_text(encoding="utf-8") if manifest_file.is_file() else "[]"
    except OSError:
        image_manifest = "[]"
    artifacts: list[dict[str, Any]] = []
    if folder.is_dir():
        try:
            artifacts = [
                {"path": file.relative_to(folder).as_posix(), "bytes": file.stat().st_size}
                for file in sorted(folder.rglob("*"))
                if file.is_file()
            ]
        except OSError:
            artifacts = []

    properties = {
        "product_id": product_id,
        "owner_username": str(payload.get("owner_username") or ""),
        "search_generated_at": str(payload.get("search_generated_at") or ""),
        "search_method": str(payload.get("search_method") or ""),
        "title": str(payload.get("title") or row.get("name") or ""),
        "name": str(row.get("name") or ""),
        "brand": str(row.get("brand") or ""),
        "source_url": str(row.get("source_url") or ""),
        "barcode": str(row.get("barcode") or ""),
        "barcode_type": str(row.get("barcode_type") or ""),
        "image_file": str(row.get("image_file") or ""),
        "origin": str(payload.get("origin") or ""),
        "category": str(payload.get("product_type") or payload.get("product_category") or ""),
        "price": str(payload.get("price") or ""),
        "compare_at_price": str(payload.get("compare_at_price") or ""),
        "currency": str(payload.get("currency") or ""),
        "availability": str(payload.get("availability") or ""),
        "rating": str(payload.get("star_rating") or payload.get("rating") or ""),
        "review_count": str(payload.get("review_count") or payload.get("rating_count") or ""),
        "audience_gender": str(research.get("audience_gender") or ""),
        "age_group": str(research.get("age_group") or ""),
        "popularity": str(research.get("popularity") or payload.get("popularity") or ""),
        "description": description,
        "search_text": search_text,
        "features_json": _json(features),
        "specs_json": _json(specs),
        "tags_json": _json(tags),
        "sources_json": _json(payload.get("sources") or []),
        "research_json": _json(research),
        "reviews_json": _json(reviews),
        "payload_json": _json(payload),
        "image_manifest_json": image_manifest,
        "artifact_manifest_json": _json(artifacts),
        "updated_at": str(row.get("updated_at") or ""),
    }
    review_rows = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        comment = _plain_text(review.get("comments") or review.get("text") or review.get("content"))
        if not comment:
            continue
        review_rows.append({
            "review_id": _review_id(product_id, review),
            "product_id": product_id,
            "author": str(review.get("nickname") or review.get("author") or ""),
            "created_at": str(review.get("created_at") or review.get("date") or ""),
            "rating": str(review.get("rating") or ""),
            "text": comment,
            "source_url": str(review.get("source_url") or ""),
        })
    properties["review_ids_json"] = _json([review["review_id"] for review in review_rows])

    entities: dict[str, list[dict[str, Any]]] = {
        "brands": ([{"product_id": product_id, "name": properties["brand"]}] if properties["brand"] else []),
        "categories": ([{"product_id": product_id, "name": properties["category"]}] if properties["category"] else []),
        "audiences": ([{"product_id": product_id, "gender": properties["audience_gender"]}] if properties["audience_gender"] else []),
        "age_groups": ([{"product_id": product_id, "name": properties["age_group"]}] if properties["age_group"] else []),
        "availability": ([{"product_id": product_id, "status": properties["availability"]}] if properties["availability"] else []),
        "tags": [{"product_id": product_id, "name": value} for value in _clean_values(tags)],
        "features": [{"product_id": product_id, "text": value, "key": hashlib.sha256(value.casefold().encode()).hexdigest()[:24]} for value in _clean_values(features)],
        "specs": [],
        "sources": [],
        "prices": [],
    }
    raw_specs = specs.items() if isinstance(specs, dict) else enumerate(specs) if isinstance(specs, list) else []
    for key, value in raw_specs:
        name = str(key).strip()
        text = _plain_text(value).strip()
        if name and text:
            entities["specs"].append({"product_id": product_id, "name": name, "value": text, "key": hashlib.sha256(f"{name.casefold()}\0{text.casefold()}".encode()).hexdigest()[:24]})

    raw_sources = payload.get("sources")
    source_candidates: list[Any] = []
    if isinstance(raw_sources, list):
        source_candidates.extend(raw_sources)
    elif isinstance(raw_sources, dict):
        source_candidates.extend({"title": key, "url": value} for key, value in raw_sources.items())
    research_sources = research.get("sources")
    if isinstance(research_sources, list):
        source_candidates.extend(research_sources)
    elif isinstance(research_sources, dict):
        source_candidates.extend({"title": key, "url": value} for key, value in research_sources.items())
    for source in source_candidates:
        if isinstance(source, str):
            source = {"url": source}
        if isinstance(source, dict):
            url = str(source.get("url") or source.get("source_url") or "").strip()
            if url:
                entities["sources"].append({"product_id": product_id, "url": url, "title": str(source.get("title") or "")})

    price_candidates = [
        ("current", payload.get("price")),
        ("compare_at", payload.get("compare_at_price")),
        ("average", research.get("average_price")),
    ]
    default_currency = str(payload.get("currency") or "").strip().upper()
    for kind, raw_price in price_candidates:
        if raw_price is None or not str(raw_price).strip():
            continue
        price_text = str(raw_price).strip()
        currency = default_currency
        currency_match = re.search(r"\b(USD|CAD|AUD|NZD|EUR|GBP|JPY|CNY|INR)\b", price_text, re.I)
        if currency_match:
            currency = currency_match.group(1).upper()
        elif not currency:
            currency = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}.get(price_text[:1], "")
        amount_match = re.search(r"\d[\d,]*(?:\.\d+)?", price_text)
        amount = amount_match.group(0).replace(",", "") if amount_match else price_text
        price_key = f"{amount}|{currency}"
        entities["prices"].append({"product_id": product_id, "amount": amount, "currency": currency, "kind": kind, "price_key": price_key, "display": price_text})

    return properties, review_rows, entities


def sync_product_ids(
    product_ids: list[str],
    db_path: Path | None = None,
    output_root: Path | None = None,
) -> None:
    """Upsert product and review nodes. Failures are logged without losing local results."""
    try:
        store = ProductStore(db_path or default_db_path())
        root = output_root or default_output_root()
        products: list[dict[str, Any]] = []
        all_reviews: list[dict[str, Any]] = []
        all_entities: dict[str, list[dict[str, Any]]] = {key: [] for key in ("brands", "categories", "audiences", "age_groups", "availability", "tags", "features", "specs", "sources", "prices")}
        for product_id in dict.fromkeys(product_ids):
            row = store.get(product_id)
            if row is None:
                continue
            product, reviews, entities = _row_for_graph(row, root)
            products.append(product)
            all_reviews.extend(reviews)
            for key, values in entities.items():
                all_entities[key].extend(values)
        if not products:
            return

        url, password = _falkor_settings()
        client = FalkorDB.from_url(
            url,
            password=password,
            socket_connect_timeout=6,
            socket_timeout=15,
        )
        try:
            graph = client.select_graph(GRAPH_NAME)
            product_query = """
            UNWIND $products AS item
            MERGE (p:Product {product_id: item.product_id})
            SET p:SearchResult
            SET p.owner_username = item.owner_username,
                p.search_generated_at = item.search_generated_at,
                p.search_method = item.search_method,
                p.title = item.title,
                p.name = item.name,
                p.brand = item.brand,
                p.source_url = item.source_url,
                p.barcode = item.barcode,
                p.barcode_type = item.barcode_type,
                p.image_file = item.image_file,
                p.origin = item.origin,
                p.category = item.category,
                p.price = item.price,
                p.compare_at_price = item.compare_at_price,
                p.currency = item.currency,
                p.availability = item.availability,
                p.rating = item.rating,
                p.review_count = item.review_count,
                p.audience_gender = item.audience_gender,
                p.age_group = item.age_group,
                p.popularity = item.popularity,
                p.description = item.description,
                p.search_text = item.search_text,
                p.features_json = item.features_json,
                p.specs_json = item.specs_json,
                p.tags_json = item.tags_json,
                p.sources_json = item.sources_json,
                p.research_json = item.research_json,
                p.reviews_json = item.reviews_json,
                p.review_ids_json = item.review_ids_json,
                p.payload_json = item.payload_json,
                p.image_manifest_json = item.image_manifest_json,
                p.artifact_manifest_json = item.artifact_manifest_json,
                p.updated_at = item.updated_at
            """
            graph.query(product_query, {"products": products}, timeout=20000)

            owned_products = [product for product in products if product["owner_username"]]
            if owned_products:
                graph.query("""
                UNWIND $products AS item
                MATCH (p:Product {product_id: item.product_id})
                MERGE (u:User {username: item.owner_username})
                SET u.username = item.owner_username
                MERGE (u)-[r:GENERATED]->(p)
                SET r.generated_at = item.search_generated_at,
                    r.search_method = item.search_method
                """, {"products": owned_products}, timeout=20000)

            # Promote useful product attributes into shared nodes so the browser can
            # visualize cross-product links and users can query them directly.
            entity_queries = {
                "brands": ("Brand", "name", "name", "MADE_BY", "name"),
                "categories": ("ProductCategory", "name", "name", "IN_CATEGORY", "name"),
                "audiences": ("Audience", "gender", "gender", "FOR_AUDIENCE", "gender"),
                "age_groups": ("AgeGroup", "name", "name", "FOR_AGE_GROUP", "name"),
                "availability": ("Availability", "status", "status", "HAS_AVAILABILITY", "status"),
                "tags": ("ProductTag", "name", "name", "HAS_TAG", "name"),
            }
            for key, (label, identity, prop, relationship, value_key) in entity_queries.items():
                items = all_entities[key]
                if not items:
                    continue
                query = f"""
                UNWIND $items AS item
                MATCH (p:Product {{product_id: item.product_id}})
                MERGE (n:{label} {{{identity}: item.{value_key}}})
                SET n.{prop} = item.{value_key}
                MERGE (p)-[:{relationship}]->(n)
                """
                graph.query(query, {"items": items}, timeout=20000)

            typed_queries = (
                ("features", "ProductFeature", "key", "text", "HAS_FEATURE"),
                ("specs", "ProductSpec", "key", "name", "value", "HAS_SPEC"),
                ("sources", "ProductSource", "url", "url", "title", "HAS_SOURCE"),
            )
            for definition in typed_queries:
                key, label, identity, *rest = definition
                relationship = rest[-1]
                value_props = rest[:-1]
                items = all_entities[key]
                if not items:
                    continue
                set_props = ", ".join(f"n.{name} = item.{name}" for name in value_props)
                query = f"""
                UNWIND $items AS item
                MATCH (p:Product {{product_id: item.product_id}})
                MERGE (n:{label} {{{identity}: item.{identity}}})
                SET {set_props}
                MERGE (p)-[:{relationship}]->(n)
                """
                graph.query(query, {"items": items}, timeout=20000)

            price_items = all_entities["prices"]
            if price_items:
                graph.query("""
                UNWIND $items AS item
                MATCH (p:Product {product_id: item.product_id})
                MERGE (n:Price {price_key: item.price_key})
                SET n.amount = item.amount, n.currency = item.currency, n.display = item.display
                MERGE (p)-[r:HAS_PRICE {kind: item.kind}]->(n)
                """, {"items": price_items}, timeout=20000)

            if all_reviews:
                review_query = """
                UNWIND $reviews AS item
                MATCH (p:Product {product_id: item.product_id})
                MERGE (r:ProductReview {review_id: item.review_id})
                SET r.product_id = item.product_id,
                    r.author = item.author,
                    r.created_at = item.created_at,
                    r.rating = item.rating,
                    r.text = item.text,
                    r.source_url = item.source_url
                MERGE (p)-[:HAS_REVIEW]->(r)
                """
                for start in range(0, len(all_reviews), 100):
                    graph.query(review_query, {"reviews": all_reviews[start:start + 100]}, timeout=20000)

            # Review IDs are kept in JSON to make Product properties queryable; use the
            # actual per-product lists here to prune reviews removed by a later refresh.
            stale_query = """
            UNWIND $products AS item
            MATCH (p:Product {product_id: item.product_id})-[:HAS_REVIEW]->(r:ProductReview)
            WHERE NOT r.review_id IN item.review_ids
            DETACH DELETE r
            """
            product_review_ids = [
                {
                    "product_id": product["product_id"],
                    "review_ids": [review["review_id"] for review in all_reviews if review["product_id"] == product["product_id"]],
                }
                for product in products
            ]
            graph.query(stale_query, {"products": product_review_ids}, timeout=20000)
        finally:
            client.close()
        LOGGER.info("Synced %d product nodes and %d review nodes to FalkorDB", len(products), len(all_reviews))
    except Exception:
        LOGGER.exception("FalkorDB sync failed; the local product record remains saved")


def delete_product_nodes(product_id: str) -> None:
    """Delete graph nodes for a locally deleted product."""
    try:
        url, password = _falkor_settings()
        client = FalkorDB.from_url(url, password=password, socket_connect_timeout=6, socket_timeout=15)
        try:
            graph = client.select_graph(GRAPH_NAME)
            graph.query(
                "MATCH (p:Product {product_id: $product_id}) DETACH DELETE p",
                {"product_id": product_id},
                timeout=10000,
            )
        finally:
            client.close()
    except Exception:
        LOGGER.exception("FalkorDB delete failed for product %s", product_id)
