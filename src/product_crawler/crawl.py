"""Fetch one product URL and write output/<uuid>/.

Scope is the product page itself, the same product's Shopify product.js
endpoint when the path is /products/{handle}, and a review feed named on
that page. When that page blocks the crawler, a local copy is downloaded
with curl_cffi, a web archive, Playwright, Scrapy, or Crawl4AI and scraped
before any other site is tried. Exa runs if those still fail. OpenAI runs
after that. Related products
are not requested.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from product_crawler import __version__
from product_crawler.extract import (
    assemble_product,
    extension_for_type,
    extract_powerreviews,
    image_bytes_ok,
    shopify_js_url,
)
from product_crawler.blocked import (
    blocked_note,
    page_matches,
    product_clues,
    rank_candidates,
    search_result_urls,
)
from product_crawler.feedback import parse_review_feed, review_feed_urls
from product_crawler.page_fetch import document_is_blocked, fetch_exa, recover_blocked_page, shell_title

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
MAX_IMAGE_BYTES = 30 * 1024 * 1024


class CrawlerError(Exception):
    pass


class PageBlocked(CrawlerError):
    """The product site refused the crawler."""


def _client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        },
        timeout=httpx.Timeout(45.0, connect=15.0),
        follow_redirects=True,
    )


def _get(client: httpx.Client, url: str) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise CrawlerError(f"request failed for {url}: {exc}") from exc
        if response.status_code in {429, 503} and attempt == 0:
            time.sleep(1.5)
            continue
        if response.status_code in {401, 403, 429} or _blocked_document(response):
            raise PageBlocked(f"{url} blocked the crawler with HTTP {response.status_code}")
        if response.status_code >= 400:
            raise CrawlerError(f"{url} returned HTTP {response.status_code}")
        return response
    raise CrawlerError(f"request failed for {url}: {last_error}")


def _fetch_product_js(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        raise CrawlerError(f"request failed for {url}: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise CrawlerError(f"{url} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise CrawlerError(f"{url} was not JSON") from exc
    if not isinstance(payload, dict):
        raise CrawlerError(f"{url} did not contain a product object")
    return payload


def _with_query(url: str, **extra: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(extra)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _fetch_json(client: httpx.Client, url: str) -> Any:
    response = _get(client, url)
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise CrawlerError(f"reviews response was not JSON ({url})") from exc


def _fetch_reviews(
    client: httpx.Client, config: dict[str, str]
) -> tuple[dict[str, Any], str]:
    endpoint = (
        "https://display.powerreviews.com/m/"
        f"{config['merchant_id']}/l/{config['locale']}/product/{config['page_id']}/reviews"
    )
    # Newest feedbacks come first. One page covers the 10 comments we keep.
    url = _with_query(endpoint, apikey=config["api_key"], **{"paging.size": "25"})
    response = _get(client, url)
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise CrawlerError(f"reviews response was not JSON ({endpoint})") from exc
    results = payload.get("results") or []
    result = results[0] if results and isinstance(results[0], dict) else {}
    rollup = result.get("rollup") if isinstance(result.get("rollup"), dict) else None
    reviews = [item for item in (result.get("reviews") or []) if isinstance(item, dict)]
    return {"rollup": rollup, "reviews": reviews}, endpoint


def _filename(position: int, source_url: str, content_type: str) -> str:
    stem = Path(urlparse(source_url).path).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    ext = extension_for_type(content_type, source_url)
    if stem.lower().endswith(ext):
        stem = stem[: -len(ext)]
    elif "." in stem:
        existing = "." + stem.rsplit(".", 1)[1].lower()
        if existing in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
            stem = stem.rsplit(".", 1)[0]
    if not stem:
        stem = f"image-{position}"
    return f"{position:02d}-{stem}{ext}"


def _download_images(
    client: httpx.Client,
    images: list[dict[str, Any]],
    folder: Path,
) -> list[dict[str, Any]]:
    image_dir = folder / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for image in images:
        source = image["src"]
        downloaded = _download_bytes(client, source)
        if downloaded is None:
            continue
        content, content_type = downloaded
        if content_type.lower().startswith("video/") or not image_bytes_ok(content, content_type):
            continue
        if len(content) > MAX_IMAGE_BYTES:
            continue
        name = _filename(image["position"], source, content_type)
        relative = f"images/{name}"
        (image_dir / name).write_bytes(content)
        manifest.append(
            {
                "filename": relative,
                "source_url": source,
                "alt": image.get("alt"),
                "position": image["position"],
            }
        )
        time.sleep(0.1)
    return manifest


def _download_bytes(client: httpx.Client, url: str) -> tuple[bytes, str] | None:
    try:
        response = _get(client, url)
    except CrawlerError:
        response = None
    if response is not None:
        return response.content, response.headers.get("content-type", "")
    try:
        from curl_cffi import requests
    except ImportError:
        return None
    try:
        fetched = requests.get(url, impersonate="chrome", timeout=45, allow_redirects=True)
    except Exception:
        return None
    if fetched.status_code >= 400:
        return None
    return fetched.content, fetched.headers.get("content-type", "")


def _blocked_document(response: httpx.Response) -> bool:
    if response.status_code >= 400:
        return False
    return document_is_blocked(response.text)


def _load_page(client: httpx.Client, url: str) -> tuple[str, str, str | None, str]:
    """Return fetched URL, HTML, a note when a different page was used, and the fetch method."""
    try:
        page = _get(client, url)
    except PageBlocked:
        return _page_after_block(client, url)
    return str(page.url), page.text, None, "httpx"


def _page_after_block(client: httpx.Client, url: str) -> tuple[str, str, str | None, str]:
    recovered = recover_blocked_page(url, client)
    if recovered is not None:
        html, fetched_url, note, method = recovered
        return fetched_url, html, note, method
    try:
        fetched, html = _alternate_page(client, url)
    except CrawlerError as exc:
        alternate_error: CrawlerError | None = exc
    else:
        return fetched, html, blocked_note(url, fetched), "alternate"
    recovered = recover_blocked_page(url, client, fetchers=[lambda: fetch_exa(url)])
    if recovered is not None:
        html, fetched_url, note, method = recovered
        return fetched_url, html, note, method
    raise alternate_error or CrawlerError(f"{url} blocked the crawler, and Exa did not return this product")


def _alternate_page(client: httpx.Client, url: str) -> tuple[str, str]:
    clues = product_clues(url)
    if not clues:
        raise CrawlerError(f"{url} blocked the crawler, and the URL had no product code to search for")
    try:
        results = client.get("https://html.duckduckgo.com/html/", params={"q": " ".join(clues[:4])})
    except httpx.HTTPError as exc:
        raise CrawlerError(f"{url} blocked the crawler, and the product search failed: {exc}") from exc
    if results.status_code >= 400:
        raise CrawlerError(f"{url} blocked the crawler, and the product search returned HTTP {results.status_code}")
    candidates = rank_candidates(search_result_urls(results.text), clues, url)[:8]
    for candidate in candidates:
        try:
            response = client.get(candidate)
        except httpx.HTTPError:
            continue
        if response.status_code >= 400 or _blocked_document(response):
            continue
        if not page_matches(response.text, clues):
            continue
        title = assemble_product(
            url,
            response.text,
            None,
            None,
            product_js_url=None,
            reviews_endpoint=None,
        ).get("title")
        if title and not shell_title(title):
            return str(response.url), response.text
    raise CrawlerError(f"{url} blocked the crawler, and no other page listed this product code")


def crawl(url: str, output_root: Path | None = None) -> Path:
    root = output_root or Path("output")
    with _client() as client:
        fetched_url, html, note, fetch_method = _load_page(client, url)
        js_url = shopify_js_url(fetched_url)
        product = _fetch_product_js(client, js_url) if js_url else None
        review_config = extract_powerreviews(html)
        reviews_payload = None
        reviews_endpoint = None
        extra_feedbacks: list[dict[str, Any]] = []
        if review_config:
            try:
                reviews_payload, reviews_endpoint = _fetch_reviews(client, review_config)
            except CrawlerError:
                reviews_payload = None
            time.sleep(0.2)
        for _kind, feed_url in review_feed_urls(html):
            try:
                extra_feedbacks.extend(parse_review_feed(_fetch_json(client, feed_url)))
            except CrawlerError:
                continue
            time.sleep(0.2)

        record = assemble_product(
            url,
            html,
            product,
            reviews_payload,
            product_js_url=js_url if product is not None else None,
            reviews_endpoint=reviews_endpoint,
            extra_feedbacks=extra_feedbacks,
        )
        if record["variant_selection"] == "query_param_not_found":
            raise CrawlerError(
                f"variant {record['requested_variant_id']} is not on this product. "
                "No output folder was created."
            )
        if not record.get("title"):
            raise CrawlerError("the page did not include a product title. No output folder was created.")
        if shell_title(record.get("title")) and note is None:
            fetched_url, html, note, fetch_method = _page_after_block(client, url)
            record = assemble_product(
                url,
                html,
                None,
                None,
                product_js_url=None,
                reviews_endpoint=None,
            )
        if shell_title(record.get("title")):
            raise CrawlerError(f"{url} blocked the crawler, and the other page had no product title")
        record["fetch_method"] = fetch_method
        if note:
            record["blocked_note"] = note
            record["fetched_url"] = fetched_url
            sources = record.get("sources") if isinstance(record.get("sources"), dict) else {}
            sources["fetched_page"] = fetched_url
            record["sources"] = sources

        images = record.get("images") or []
        run_id = str(uuid.uuid4())
        folder = root / run_id
        folder.mkdir(parents=True, exist_ok=False)
        try:
            (folder / "source-page.html").write_text(html, encoding="utf-8")
            manifest = _download_images(client, images, folder) if images else []
            if images and not manifest:
                raise CrawlerError(f"no images were saved for {url}")
            record["run_id"] = run_id
            record["image_count"] = len(manifest)
            from product_crawler.openai_enrich import finish_record

            record, extra_images = finish_record(record, has_image=bool(manifest), html_text=html, page_url=fetched_url)
            if extra_images:
                manifest.extend(_download_images(client, extra_images, folder))
                record["image_count"] = len(manifest)
            (folder / "product.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (folder / "image-manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
    return folder.resolve()
