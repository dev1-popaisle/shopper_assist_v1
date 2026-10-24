"""Create one product from a text description using only the OpenAI API."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from product_crawler.crawl import _client, _download_images
from product_crawler.extract import (
    _jsonld_gtin,
    _jsonld_images,
    _jsonld_nodes,
    _jsonld_product,
    _labeled_barcode,
    allowed_image_url,
)
from product_crawler.openai_enrich import _market_barcode, cited_product_pages, search_by_prompt
from product_crawler.page_fetch import document_is_blocked, shell_title

_SKIP_IMAGE = ("favicon", "logo", "icon", "sprite", "badge", "banner", "placeholder", "tempfile", "brand.png")
_TITLE_STOP = {
    "true",
    "wireless",
    "with",
    "inch",
    "the",
    "and",
    "for",
    "black",
    "white",
    "case",
    "charging",
    "lighting",
    "gaming",
    "monitor",
    "earbuds",
}


def create_from_prompt(prompt: str, output_root: Path) -> Path:
    text = prompt.strip()
    if not text:
        raise ValueError("Describe the product to search for.")
    if len(text) > 2000:
        raise ValueError("Describe the product in 2000 characters or less.")
    record, images = search_by_prompt(text)
    run_id = str(uuid.uuid4())
    record["run_id"] = run_id
    record["origin"] = "openai_prompt"
    record["search_prompt"] = text
    folder = output_root / run_id
    folder.mkdir(parents=True, exist_ok=False)
    try:
        _write_prompt_folder(record, images, folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder


def rewrite_prompt_folder(folder: Path) -> None:
    """Add a photo and barcode to an existing OpenAI product without changing its name."""
    path = folder / "product.json"
    current = json.loads(path.read_text(encoding="utf-8"))
    current["origin"] = "openai_prompt"
    current["fetch_method"] = "openai"
    _write_prompt_folder(current, [], folder)


def _write_prompt_folder(record: dict[str, Any], images: list[dict[str, Any]], folder: Path) -> None:
    with _client() as client:
        manifest = _first_saved_image(client, images, folder)
        if not manifest or not record.get("barcode"):
            manifest = _fill_from_cited_pages(client, record, folder, manifest)
    record["image_count"] = len(manifest)
    record["images"] = [
        {
            "position": item["position"],
            "src": item["source_url"],
            "alt": record.get("title"),
            "width": None,
            "height": None,
        }
        for item in manifest
    ]
    (folder / "product.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (folder / "image-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _fill_from_cited_pages(
    client: httpx.Client,
    record: dict[str, Any],
    folder: Path,
    manifest: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read a photo file and a printed barcode from pages the web search opened."""
    for page_url in cited_product_pages(record):
        html = _fetch_html(client, page_url)
        if not html or not page_matches_product(html, page_url, record):
            continue
        code = printed_barcode(html)
        if code and not record.get("barcode"):
            record["barcode"] = code
            record["barcode_note"] = f"Printed on {urlparse(page_url).netloc}"
        if not record.get("requested_url"):
            record["requested_url"] = page_url
        if not manifest:
            photos = [
                {"position": index, "src": url, "alt": record.get("title")}
                for index, url in enumerate(photo_urls(html, page_url), start=1)
            ]
            manifest = _first_saved_image(client, photos, folder)
        if manifest and record.get("barcode"):
            break
    return manifest


def page_matches_product(html: str, page_url: str, record: dict[str, Any]) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if shell_title(title) or document_is_blocked(html):
        return False
    blob = f"{title} {page_url}".lower()
    product_generation = _generation(str(record.get("title") or ""))
    page_generation = _generation(title)
    if product_generation and page_generation and product_generation != page_generation:
        return False
    brand = str(record.get("brand") or "").strip().lower()
    if brand and brand not in blob and brand not in html[:12000].lower():
        return False
    tokens = _model_tokens(str(record.get("title") or ""), brand)
    return not tokens or any(token in blob for token in tokens)


def printed_barcode(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    labeled = _labeled_barcode(text)
    if labeled:
        return labeled
    product = _jsonld_product(_jsonld_nodes(soup))
    return _market_barcode(_jsonld_gtin(product))


def photo_urls(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    product = _jsonld_product(_jsonld_nodes(soup))
    found: list[str] = [item["src"] for item in _jsonld_images(product, page_url)]
    normalized = html.replace("\\u002F", "/").replace("\\/", "/")
    found.extend(re.findall(r"https://[^\"'\s>]+\.(?:jpg|jpeg|png|webp|avif)", normalized, re.I))
    photos: list[str] = []
    for url in found:
        path = urlparse(url).path.lower()
        if any(word in path for word in _SKIP_IMAGE):
            continue
        if not allowed_image_url(url):
            continue
        if url not in photos:
            photos.append(url)
    return photos[:8]


def _model_tokens(title: str, brand: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[a-z0-9]{4,}", title.lower()):
        if token == brand or token in _TITLE_STOP:
            continue
        tokens.append(token)
    return tokens[:6]


def _generation(text: str) -> int | None:
    lowered = text.lower()
    if re.search(r"\b3rd\b|\bpro\s*3\b", lowered):
        return 3
    if re.search(r"\b2nd\b|\bpro\s*2\b", lowered):
        return 2
    return None


def _fetch_html(client: httpx.Client, url: str) -> str | None:
    try:
        response = client.get(url, timeout=12.0)
    except httpx.HTTPError:
        response = None
    if response is not None and response.status_code < 400:
        content_type = response.headers.get("content-type", "")
        if "html" in content_type.lower() and not document_is_blocked(response.text):
            return response.text
    try:
        from curl_cffi import requests
    except ImportError:
        return None
    try:
        fetched = requests.get(url, impersonate="chrome", timeout=12, allow_redirects=True)
    except Exception:
        return None
    if fetched.status_code >= 400 or document_is_blocked(fetched.text):
        return None
    if "html" not in fetched.headers.get("content-type", "").lower() and not fetched.text.lstrip().startswith("<"):
        return None
    return fetched.text


def _first_saved_image(client: Any, images: list[dict[str, Any]], folder: Path) -> list[dict[str, Any]]:
    for image in images:
        manifest = _download_images(client, [image], folder)
        if manifest:
            return manifest
    return []
