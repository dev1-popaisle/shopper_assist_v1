"""Fill gaps the page crawl missed, then research the product.

This runs only when required product facts or product research are missing.
Video lookup is limited to public descriptions and tags; it does not open or
inspect video pages.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from product_crawler.extract import latest_feedbacks

MODEL = "gpt-4.1-mini"
_BARCODE_LENGTHS = {8, 12, 13, 14}
_VIDEO_CATEGORIES = (
    "product explanation",
    "visual feedback",
    "how to use",
    "health benefits",
    "how-to",
    "do it yourself",
)
_RESEARCH_FIELDS = (
    "audience_gender",
    "popularity",
    "age_group",
    "professionals",
    "demographic_appeal",
    "top_us_state",
    "top_us_city",
    "health_benefits",
    "average_price",
    "bundle",
    "recalls",
)


def missing_required(record: dict[str, Any], *, has_image: bool) -> list[str]:
    """Names of required product facts this record does not have yet."""
    missing: list[str] = []
    if not _text(record.get("brand")):
        missing.append("brand")
    if _market_barcode(record.get("barcode")) is None:
        missing.append("barcode")
    if not _text(record.get("title")):
        missing.append("product name")
    if not has_image:
        missing.append("product image")
    reviews = record.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        missing.append("user feedback")
    return missing


def apply_findings(
    record: dict[str, Any],
    findings: dict[str, Any],
    missing: list[str],
) -> list[dict[str, Any]]:
    """Copy only the missing facts onto the record. Return image dicts to download."""
    if "brand" in missing and _text(findings.get("brand")):
        record["brand"] = str(findings["brand"]).strip()
    if "barcode" in missing:
        barcode = _barcode_from_findings(findings)
        if barcode:
            record["barcode"] = barcode
            note = findings.get("barcode_note")
            if isinstance(note, str) and note.strip():
                record["barcode_note"] = note.strip()
            if record.get("origin") != "openai_prompt":
                record["barcode_from_openai"] = True
    if "product name" in missing and _text(findings.get("product_name")):
        record["title"] = str(findings["product_name"]).strip()
    images: list[dict[str, Any]] = []
    if "product image" in missing and _http(findings.get("image_url")):
        images.append(
            {
                "position": 1,
                "src": str(findings["image_url"]).strip(),
                "alt": record.get("title"),
                "width": None,
                "height": None,
            }
        )
        record["images"] = images
    if "user feedback" in missing:
        reviews = latest_feedbacks(_feedback_items(findings.get("feedbacks")))
        if reviews:
            record["reviews"] = reviews
            record["reviews_note"] = None
    record["research"] = _research(findings)
    record["openai_enrichment"] = {"status": "completed", "missing": missing}
    return images


def apply_prompt_findings(prompt: str, findings: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a product record in the same shape as a URL crawl, from an OpenAI web search."""
    record = _prompt_record(prompt)
    images = apply_findings(record, findings, missing_required(record, has_image=False))
    if not _text(record.get("title")):
        record["title"] = prompt.strip()[:180]
    _copy_prompt_details(record, findings)
    videos = product_videos(findings.get("videos"))
    if videos:
        record["videos"] = videos[:2]
        record["videos_note"] = None
    return record, _image_entries(record.get("title"), image_urls(findings)) or images


def search_by_prompt(prompt: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Search the public web for a described product using only the OpenAI API."""
    key = load_api_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY_AVATAR is not in api_config.json")
    findings = _request_with_retry(_prompt_only_prompt(prompt), key, wide=True)
    record, images = apply_prompt_findings(prompt, findings)
    if record.get("barcode") is None or not _direct_image(images):
        extra = _request_with_retry(_gap_prompt(record), key, wide=True)
        record, images = _merge_gap(record, images, extra)
    if not isinstance(record.get("videos"), list):
        _attach_videos(record, key)
    else:
        record["videos"] = product_videos(record["videos"])[:2]
        record["videos_note"] = None if record["videos"] else "No related video descriptions or tags found."
    return record, images


def search_photo_and_barcode(record: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Second web search when the saved file still has no photo or no barcode."""
    key = load_api_key()
    if not key:
        return record, []
    extra = _request_with_retry(_gap_prompt(record), key, wide=True)
    return _merge_gap(record, [], extra)


def cited_product_pages(record: dict[str, Any]) -> list[str]:
    """Pages the OpenAI web search actually opened for this product."""
    key = load_api_key()
    if not key:
        return []
    prompt = f"""Search the public web and open retailer or manufacturer pages for this exact product.
Prefer a page whose specification table prints the UPC, EAN, or GTIN, and a page that shows the product photo.
Name: {record.get("title")}
Brand: {record.get("brand")}
Description: {record.get("search_prompt")}
Start the reply with {{ and return only JSON: {{"product_page_url": "https://..."}}
"""
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "tools": [{"type": "web_search", "search_context_size": "high"}],
                "include": ["web_search_call.action.sources"],
                "input": prompt,
                "max_output_tokens": 800,
            },
            timeout=120,
        )
    except httpx.HTTPError:
        return []
    if response.status_code >= 400:
        return []
    return _source_urls(response.json())


def finish_record(record: dict[str, Any], *, has_image: bool, html_text: str | None = None, page_url: str = "") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fill crawl gaps, attach research, and look up one or two product videos."""
    missing = missing_required(record, has_image=has_image)
    need_research = bool(missing) or not isinstance(record.get("research"), dict)
    need_videos = not isinstance(record.get("videos"), list)
    if not need_research and not need_videos:
        return record, []
    key = load_api_key()
    if not key:
        record["openai_enrichment"] = {
            "status": "skipped",
            "missing": missing,
            "error": "OPENAI_API_KEY_AVATAR is not in api_config.json",
        }
        return record, []
    images: list[dict[str, Any]] = []
    if need_research:
        try:
            findings = research_product(record, missing, key)
        except Exception as exc:
            record["openai_enrichment"] = {
                "status": "failed",
                "missing": missing,
                "error": _safe_error(exc, key),
            }
        else:
            images = apply_findings(record, findings, missing)
    if need_videos:
        _attach_videos(record, key)
    return record, images


def product_videos(value: Any) -> list[dict[str, Any]]:
    """Keep descriptions and tags from at most two relevant video search results."""
    if not isinstance(value, list):
        return []
    videos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _video_url(item.get("url"))
        title = item.get("title")
        title = title.strip() if isinstance(title, str) else ""
        description = str(item.get("description") or "").strip()
        raw_tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        tags = [tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()][:12]
        identity = url or f"{title}:{description}"
        if identity in seen:
            continue
        seen.add(identity)
        videos.append(
            {
                "url": url,
                "title": title or "Video search result",
                "description": description,
                "tags": tags,
                "category": _video_category(item.get("category")),
            }
        )
        if len(videos) == 2:
            break
    return videos


def research_product(record: dict[str, Any], missing: list[str], api_key: str) -> dict[str, Any]:
    """Search the public web. A second pass looks past the crawled URL for gaps."""
    wide = any(name in missing for name in ("barcode", "brand", "product name"))
    findings = _request_with_retry(_prompt(record, missing), api_key, wide=wide)
    left = unfilled(findings, missing)
    if not left:
        return findings
    try:
        extra = _request_with_retry(_offsite_prompt(record, left), api_key, wide=True)
    except RuntimeError:
        return findings
    return merge_offsite(findings, extra, left)


def _request_with_retry(prompt: str, api_key: str, *, wide: bool) -> dict[str, Any]:
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            return _request_findings(prompt, api_key, wide=wide)
        except RuntimeError as exc:
            last_error = exc
    raise last_error or RuntimeError("OpenAI research failed")


def unfilled(findings: dict[str, Any], missing: list[str]) -> list[str]:
    """Required facts the first web search still did not return."""
    left: list[str] = []
    if "brand" in missing and not _text(findings.get("brand")):
        left.append("brand")
    if "barcode" in missing and _barcode_from_findings(findings) is None:
        left.append("barcode")
    if "product name" in missing and not _text(findings.get("product_name")):
        left.append("product name")
    if "product image" in missing and not _http(findings.get("image_url")):
        left.append("product image")
    if "user feedback" in missing and not _feedback_items(findings.get("feedbacks")):
        left.append("user feedback")
    return left


def merge_offsite(
    findings: dict[str, Any],
    extra: dict[str, Any],
    left: list[str],
) -> dict[str, Any]:
    """Keep the first search, and copy only the gaps a wider search filled."""
    merged = dict(findings)
    if "brand" in left and _text(extra.get("brand")):
        merged["brand"] = str(extra["brand"]).strip()
    if "barcode" in left and _barcode_from_findings(extra):
        merged["barcode"] = _barcode_from_findings(extra)
        if isinstance(extra.get("barcode_note"), str) and extra["barcode_note"].strip():
            merged["barcode_note"] = extra["barcode_note"].strip()
    if "product name" in left and _text(extra.get("product_name")):
        merged["product_name"] = str(extra["product_name"]).strip()
    if "product image" in left and _http(extra.get("image_url")):
        merged["image_url"] = str(extra["image_url"]).strip()
    if "user feedback" in left and _feedback_items(extra.get("feedbacks")):
        merged["feedbacks"] = extra.get("feedbacks")
    first = findings.get("sources") if isinstance(findings.get("sources"), list) else []
    second = extra.get("sources") if isinstance(extra.get("sources"), list) else []
    merged["sources"] = [*first, *[url for url in second if url not in first]]
    return merged


def _request_findings(prompt: str, api_key: str, *, wide: bool = False, timeout: float = 120) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "web_search"}
    if wide:
        tool["search_context_size"] = "high"
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "tools": [tool],
            "input": prompt,
            "max_output_tokens": 4000,
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        detail = response.text[:300]
        raise RuntimeError(f"OpenAI returned HTTP {response.status_code}: {detail}")
    return _parse_findings(response.json())


def load_api_key() -> str | None:
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = payload.get("OPENAI_API_KEY_AVATAR") if isinstance(payload, dict) else None
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def _prompt(record: dict[str, Any], missing: list[str]) -> str:
    known = _identity(record)
    return f"""You are completing a product record. Search the public web for this specific product.
The source URL only identifies the product. The search is not limited to the source URL.
When brand, barcode, product name, a product image, or buyer feedback is missing, search other public pages: the manufacturer, other retailers, and product specification tables.
Do not invent a barcode, price, place, recall, or buyer comment.
If a fact is not supported by a public page, set that field to "Unknown".

Already gathered:
{json.dumps(known, ensure_ascii=False)}

Still missing from the page crawl: {", ".join(missing) if missing else "nothing"}.
Fill brand, barcode, product_name, image_url, and feedbacks only when you find them on a public page, including pages other than the source URL.
barcode must be a GTIN, UPC, or EAN (8, 12, 13, or 14 digits) or null.
A style code, SKU, or product number is not a barcode.
If the source URL has no GTIN, UPC, or EAN, search other sites for this same product using its name, brand, style, and SKU.
When a retailer spec table or Shopify barcode field prints digits for this exact style, put those digits in barcode.
If that table gives one size's UPC for the style, still put those digits in barcode and name the size in barcode_note.
image_url must be one direct https URL of a still product photo, or null.
feedbacks must be real buyer comments, at most 10, each comments field at most 100 words.

Also answer this product research from public information:
1. audience_gender: who the product is sold for (men, women, unisex, or both).
2. popularity: whether it is popular in the US, worldwide, both, or neither.
3. age_group: the age group it is for or commonly purchased by.
4. professionals: which professions or serious users are known to use it.
5. demographic_appeal: any specific demographic appeal.
6. top_us_state and top_us_city: where in the US it is sold most, and a city in that state when a public source says so.
7. health_benefits: health benefits supported for this product, or Unknown.
8. average_price: the average market price today, with currency.
9. bundle: what buyers often purchase with it, or a sensible bundle.
10. recalls: major recalls or widely reported public complaints. If none are found, say none found.

Start the reply with {{ and return only a JSON object with these keys:
brand, barcode, barcode_note, product_name, image_url, feedbacks, research, sources.
feedbacks is a list of objects with nickname, created_at, rating, comments.
research is an object with audience_gender, popularity, age_group, professionals, demographic_appeal, top_us_state, top_us_city, health_benefits, average_price, bundle, recalls.
sources is a list of URLs you used. Include pages other than the source URL when you used them.
barcode_note is a short note naming the size or style the barcode belongs to, or null.
"""


def _prompt_only_prompt(prompt: str) -> str:
    return f"""A person described a product. Search the public web and identify the one product that matches this description.
This search uses only your web search. Do not assume a page was already crawled.
Do not invent a barcode, price, place, recall, or buyer comment.
If a fact is not supported by a public page, use null for barcode and image_url, and "Unknown" for research answers.

Description:
{prompt.strip()}

barcode must be a GTIN, UPC, or EAN printed for this exact product (8, 12, 13, or 14 digits), or null.
Search retailer spec tables for the words UPC, EAN, and GTIN. A style code, SKU, or store product number is not a barcode.
product_page_url must be one public product page for this exact product.
image_url and image_urls must be direct image files whose paths end in .jpg, .jpeg, .png, or .webp.
Do not return an HTML product page as an image. If you cannot find a real image file, use null and an empty list.
price is the current selling price as digits, or null. currency is the price currency, such as USD.
description is a short product description from a public page.
features is a list of short feature lines from a public page.
sku and product_id are the manufacturer's identifiers when a page prints them, or null.
feedbacks must be real buyer comments, at most 10, each comments field at most 100 words.
Also answer this product research from public information:
1. audience_gender: who the product is sold for (men, women, unisex, or both).
2. popularity: whether it is popular in the US, worldwide, both, or neither.
3. age_group: the age group it is for or commonly purchased by.
4. professionals: which professions or serious users are known to use it.
5. demographic_appeal: any specific demographic appeal.
6. top_us_state and top_us_city: where in the US it is sold most, and a city in that state when a public source says so.
7. health_benefits: health benefits supported for this product, or Unknown.
8. average_price: the average market price today, with currency.
9. bundle: what buyers often purchase with it, or a sensible bundle.
10. recalls: major recalls or widely reported public complaints. If none are found, say none found.

Start the reply with {{ and return only a JSON object with these keys:
brand, barcode, barcode_note, product_name, product_page_url, sku, product_id, price, currency, description, features, image_url, image_urls, feedbacks, research, sources.
feedbacks is a list of objects with nickname, created_at, rating, comments.
features is a list of strings.
image_urls is a list of direct image-file URLs.
research is an object with audience_gender, popularity, age_group, professionals, demographic_appeal, top_us_state, top_us_city, health_benefits, average_price, bundle, recalls.
sources is a list of URLs you used.
"""


def _gap_prompt(record: dict[str, Any]) -> str:
    known = {
        "description": record.get("search_prompt"),
        "product_name": record.get("title"),
        "brand": record.get("brand"),
        "product_page_url": record.get("requested_url"),
    }
    return f"""The product file still needs a barcode and a downloadable product photo.
Search retailer and manufacturer pages. Open the specification table.

Product:
{json.dumps(known, ensure_ascii=False)}

barcode must be a GTIN, UPC, or EAN printed for this exact product (8, 12, 13, or 14 digits), or null.
Search for the product name plus UPC, EAN, and GTIN. Do not invent digits. A style code or SKU is not a barcode.
image_urls must be up to 3 https URLs of a product photo file that is online now.
Each path must end in .jpg, .jpeg, .png, or .webp. Copy the URL from the page. Do not invent a filename.
Do not return an HTML page, a PDF, or a URL whose file name is a placeholder.
product_page_url is one public page for this exact product.

Start the reply with {{ and return only JSON:
{{"barcode": null, "barcode_note": null, "product_page_url": "https://...", "image_urls": ["https://.../photo.jpg"], "sources": ["https://..."]}}
"""


def _prompt_record(prompt: str) -> dict[str, Any]:
    return {
        "requested_url": None,
        "title": None,
        "brand": None,
        "product_id": None,
        "handle": None,
        "product_type": None,
        "requested_variant_id": None,
        "variant_selection": "none",
        "selected_variant": None,
        "price": None,
        "compare_at_price": None,
        "currency": None,
        "on_sale": False,
        "availability": None,
        "sku": None,
        "barcode": None,
        "description": {"text": None, "html": None},
        "features": [],
        "description_points": [],
        "dimensions": None,
        "specs": {},
        "spec_icons": [],
        "materials": [],
        "weight": None,
        "ranking": None,
        "popularity": None,
        "star_rating": None,
        "review_count": None,
        "rating_count": None,
        "rating": None,
        "reviews": [],
        "reviews_note": "No user feedback found on this page.",
        "breadcrumbs": [],
        "options": [],
        "attributes_from_tags": {},
        "tags": [],
        "variants": [],
        "images": [],
        "published_at": None,
        "created_at": None,
        "sources": {"product_page": None, "product_js": None, "reviews": None},
        "origin": "openai_prompt",
        "search_prompt": prompt.strip(),
        "fetch_method": "openai",
    }


def _copy_prompt_details(record: dict[str, Any], findings: dict[str, Any]) -> None:
    page = findings.get("product_page_url")
    if _http(page):
        record["requested_url"] = str(page).strip()
        sources = record.get("sources") if isinstance(record.get("sources"), dict) else {}
        sources["product_page"] = record["requested_url"]
        record["sources"] = sources
    for key in ("sku", "product_id", "currency"):
        if _text(findings.get(key)):
            record[key] = str(findings[key]).strip()
    price = findings.get("price")
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        record["price"] = str(price)
    elif _text(price):
        record["price"] = str(price).strip()
    if _text(findings.get("description")):
        text = str(findings["description"]).strip()
        record["description"] = {"text": text, "html": None}
    features = findings.get("features")
    if isinstance(features, list):
        record["features"] = [str(item).strip() for item in features if _text(item)]
        record["description_points"] = list(record["features"])


def _direct_image(images: list[dict[str, Any]]) -> bool:
    return any(_usable_image_url(str(item.get("src") or "")) for item in images)


def _usable_image_url(url: str) -> bool:
    path = url.lower().split("?", 1)[0]
    if not path.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
        return False
    name = path.rsplit("/", 1)[-1]
    return "xxxx" not in name and "x0x0" not in name


def image_urls(findings: dict[str, Any]) -> list[str]:
    found: list[str] = []
    candidates = [findings.get("image_url")]
    many = findings.get("image_urls")
    if isinstance(many, list):
        candidates.extend(many)
    for item in candidates:
        if not _http(item):
            continue
        url = str(item).strip()
        if _usable_image_url(url) and url not in found:
            found.append(url)
    return found


def _image_entries(title: Any, urls: list[str]) -> list[dict[str, Any]]:
    return [
        {"position": index, "src": url, "alt": title, "width": None, "height": None}
        for index, url in enumerate(urls, start=1)
    ]


def _merge_gap(
    record: dict[str, Any],
    images: list[dict[str, Any]],
    extra: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if record.get("barcode") is None:
        code = _barcode_from_findings(extra)
        if code:
            record["barcode"] = code
            note = extra.get("barcode_note")
            if isinstance(note, str) and note.strip():
                record["barcode_note"] = note.strip()
            if record.get("origin") != "openai_prompt":
                record["barcode_from_openai"] = True
    if _http(extra.get("product_page_url")) and not _http(record.get("requested_url")):
        _copy_prompt_details(record, extra)
    urls = image_urls(extra)
    merged = list(images)
    seen = {item.get("src") for item in merged}
    for entry in _image_entries(record.get("title"), urls):
        if entry["src"] not in seen:
            merged.append(entry)
            seen.add(entry["src"])
    record["images"] = merged
    return record, merged


def _video_prompt(record: dict[str, Any]) -> str:
    known = {
        "product_name": record.get("title"),
        "brand": record.get("brand"),
        "barcode": record.get("barcode"),
    }
    categories = ", ".join(_VIDEO_CATEGORIES)
    return f"""Search public video search result metadata for descriptions and tags about this exact product.
Use the brand, product name, and UPC together. Use only published descriptions, tags, and search result snippets. Do not open, watch, transcribe, or analyze any video or video page.
Categories to choose from: {categories}.

Product:
{json.dumps(known, ensure_ascii=False)}

Return at most 2 metadata entries. A source URL is optional and must come from the search results; do not open or validate it. Do not invent a URL. If no description or tags are found, return an empty list.

Start the reply with {{ and return only JSON:
{{"videos": [{{"url": "https://...", "title": "short title", "description": "published description or search snippet", "tags": ["tag"], "category": "one category from the list"}}]}}
"""


def _offsite_prompt(record: dict[str, Any], left: list[str]) -> str:
    known = _identity(record)
    return f"""The crawl and the first web search did not find: {", ".join(left)}.
Search the whole public web. Do not stop at the source URL and do not answer from that page alone.
Open the manufacturer page and other retailers. Match this product by name, brand, style, and SKU.

Product:
{json.dumps(known, ensure_ascii=False)}

barcode must be a GTIN, UPC, or EAN printed for this exact product (8, 12, 13, or 14 digits), or null.
A style code, SKU, or store product number is not a barcode.
Search queries should include the style or SKU plus UPC, EAN, and GTIN.
If sizes have different barcodes, return the one a public page prints for this style and name that size in barcode_note.
brand and product_name must name this same product. image_url must be a direct https photo from any public page.
feedbacks must be real buyer comments from any public page, at most 10.

Start the reply with {{ and return only JSON with these keys:
brand, barcode, barcode_note, product_name, image_url, feedbacks, sources.
"""


def _identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": record.get("requested_url"),
        "product_name": record.get("title"),
        "brand": record.get("brand"),
        "product_id": record.get("product_id"),
        "sku": _sku(record),
        "image": _image_url(record),
        "barcode": record.get("barcode"),
        "price": record.get("price"),
        "currency": record.get("currency"),
    }


def _image_url(record: dict[str, Any]) -> str | None:
    images = record.get("images")
    if not isinstance(images, list):
        return None
    for image in images:
        if isinstance(image, dict) and _http(image.get("src")):
            return str(image["src"]).strip()
    return None


def _sku(record: dict[str, Any]) -> str | None:
    if _text(record.get("sku")):
        return str(record["sku"]).strip()
    url = record.get("requested_url")
    if not isinstance(url, str):
        return None
    for key in ("sku", "style", "styleId"):
        values = parse_qs(urlparse(url).query).get(key) or []
        if values and isinstance(values[0], str) and values[0].strip():
            return values[0].strip()
    return None


def _source_urls(payload: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str) or not value.startswith("http"):
            return
        parts = urlsplit(value)
        query = urlencode([(key, item) for key, item in parse_qsl(parts.query) if key != "utm_source"])
        cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
        if cleaned not in found:
            found.append(cleaned)

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if isinstance(action, dict):
            for source in action.get("sources") or []:
                if isinstance(source, dict):
                    add(source.get("url"))
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            for note in part.get("annotations") or []:
                if isinstance(note, dict):
                    add(note.get("url"))
    try:
        findings = _parse_findings(payload)
    except RuntimeError:
        findings = {}
    add(findings.get("product_page_url"))
    for source in findings.get("sources") or []:
        add(source)
    return found[:8]


def _parse_findings(payload: dict[str, Any]) -> dict[str, Any]:
    texts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    raw = "\n".join(texts).strip()
    if not raw:
        raise RuntimeError("OpenAI returned no text")
    parsed = _json_object(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI did not return a JSON object")
    return parsed


def _json_object(raw: str) -> Any:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    candidate = fenced.group(1) if fenced else raw
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("OpenAI text did not contain JSON")
    return json.loads(candidate[start : end + 1])


def _feedback_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict) or not _text(entry.get("comments")):
            continue
        created = entry.get("created_at")
        items.append(
            {
                "nickname": entry.get("nickname") if _text(entry.get("nickname")) else None,
                "created_at": created.strip() if isinstance(created, str) else None,
                "rating": entry.get("rating") if isinstance(entry.get("rating"), (int, float)) else None,
                "headline": None,
                "comments": str(entry["comments"]).strip(),
                "source": "openai",
            }
        )
    return items


def _research(findings: dict[str, Any]) -> dict[str, Any]:
    source = findings.get("research") if isinstance(findings.get("research"), dict) else {}
    answers = {name: _answer(source.get(name)) for name in _RESEARCH_FIELDS}
    urls = findings.get("sources")
    answers["sources"] = [url for url in urls if isinstance(url, str) and url.startswith("http")] if isinstance(urls, list) else []
    return answers


def _answer(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return "Unknown"


def _embedded_page_videos(html_text: str, page_url: str = "") -> list[dict[str, str]]:
    """Read video references already present in local HTML; never fetch video pages."""
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    found = []
    for frame in soup.select("iframe[src]"):
        url = frame.get("src") or ""
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/") and page_url:
            url = urljoin(page_url, url)
        parsed = product_videos([{"url": url, "title": frame.get("title")}])
        found.extend(parsed)
    return found[:2]


def _attach_videos(record: dict[str, Any], api_key: str, html_text: str | None = None, page_url: str = "") -> None:
    try:
        findings = _request_findings_bounded(_video_prompt(record), api_key, timeout=10)
        found = product_videos(findings.get("videos"))[:2]
        record["videos"] = found
        record["videos_note"] = None if found else "No related video descriptions or tags found."
    except Exception as exc:
        record["videos"] = []
        record["videos_note"] = "Video description and tag search timed out or failed: " + _safe_error(exc, api_key)


def _request_findings_bounded(prompt: str, api_key: str, *, timeout: float) -> dict[str, Any]:
    """Return at the deadline even if the remote metadata search stalls."""
    finished = threading.Event()
    result: list[dict[str, Any]] = []
    errors: list[Exception] = []

    def request() -> None:
        try:
            result.append(_request_findings(prompt, api_key, wide=True, timeout=timeout))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    threading.Thread(target=request, daemon=True, name="video-metadata-search").start()
    if not finished.wait(timeout):
        raise TimeoutError(f"video metadata search exceeded {timeout:g} seconds")
    if errors:
        raise errors[0]
    return result[0]


def _video_url(value: Any) -> str | None:
    if not _http(value):
        return None
    url = str(value).strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path
    if host in {"youtube.com", "m.youtube.com"} and (path == "/watch" or path.startswith("/shorts/") or path.startswith("/embed/")):
        return url
    if host == "youtu.be" and path.strip("/"):
        return url
    if host == "vimeo.com" and path.strip("/").split("/")[0].isdigit():
        return url
    if host in {"dailymotion.com", "tiktok.com"} and "/video/" in path:
        return url
    if "/video/" in path or path.lower().endswith((".mp4", ".webm")):
        return url
    return None


def _video_category(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _VIDEO_CATEGORIES:
            return text
    return "product explanation"


def _barcode_from_findings(findings: dict[str, Any]) -> str | None:
    code = _market_barcode(findings.get("barcode"))
    if code:
        return code
    for key in ("barcode_note", "quote"):
        note = findings.get(key)
        if isinstance(note, str) and re.search(r"\b(upc|ean|gtin|barcode)\b", note, re.I):
            code = _market_barcode(note)
            if code:
                return code
    return None


def _market_barcode(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) in _BARCODE_LENGTHS:
        return digits
    return None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _http(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(("http://", "https://"))


def _safe_error(exc: Exception, api_key: str) -> str:
    text = str(exc).replace(api_key, "[redacted]")
    return text[:500]
