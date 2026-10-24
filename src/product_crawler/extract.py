"""Turn a product page, Shopify product.js, and reviews payload into a record.

These helpers do not fetch anything. Callers pass the already downloaded
bodies for the single product URL.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from product_crawler.feedback import _usable_comment, feedbacks_from_html

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}

MATERIAL_RE = re.compile(
    r"\b(mesh|rubber|foam|eva|sockliner|silicone|leather|textile|nylon|"
    r"polyester|cotton|wool|suede|canvas|knit|knitted|tpu|synthetic|"
    r"gore-tex|merino)\b",
    re.I,
)

MEASUREMENT_RE = re.compile(
    r"^(?P<label>weight|heel-to-toe drop|stability|cushion)\s*:?\s*(?P<value>.+)$",
    re.I,
)

LWH_RE = re.compile(
    r"(?P<l>\d+(?:\.\d+)?)\s*[x×]\s*(?P<w>\d+(?:\.\d+)?)\s*[x×]\s*"
    r"(?P<h>\d+(?:\.\d+)?)\s*(?P<unit>in|cm|mm|inches)?\b",
    re.I,
)

WEIGHT_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>oz|lb|lbs|g|kg)\b",
    re.I,
)


def cents_to_price(cents: Any) -> str | None:
    """Shopify product.js stores prices as integer cents."""
    if cents is None or cents == "":
        return None
    return f"{int(cents) / 100:.2f}"


def absolute_https(url: str, base: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)


def allowed_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in IMAGE_EXTENSIONS)


def image_bytes_ok(content: bytes, content_type: str) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    if ct == "image/jpg":
        ct = "image/jpeg"
    if len(content) < 32:
        return False
    allowed = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/avif",
        "application/octet-stream",
        "binary/octet-stream",
        "",
    }
    if ct not in allowed:
        return False
    if content.startswith(b"\xff\xd8\xff"):
        return ct in {"image/jpeg", "application/octet-stream", "binary/octet-stream", ""}
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ct in {"image/png", "application/octet-stream", "binary/octet-stream", ""}
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ct in {"image/webp", "application/octet-stream", "binary/octet-stream", ""}
    if ct == "image/avif" and b"ftyp" in content[:32] and b"avif" in content[:32]:
        return True
    return False


def extension_for_type(content_type: str, url: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    mapped = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }
    if ct in mapped:
        return mapped[ct]
    suffix = urlparse(url).path.lower()
    for ext in IMAGE_EXTENSIONS:
        if suffix.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".img"


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    lines = []
    for line in soup.get_text("\n", strip=True).splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def description_points(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    points = []
    for li in soup.find_all("li"):
        text = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
        if text:
            points.append(text)
    return points


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def _meta(soup: BeautifulSoup, *, prop: str | None = None, name: str | None = None) -> str | None:
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
    else:
        tag = soup.find("meta", attrs={"name": name})
    if not tag:
        return None
    content = tag.get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


def extract_breadcrumbs(soup: BeautifulSoup, page_url: str) -> list[dict[str, str]]:
    crumbs = []
    for anchor in soup.select(".breadcrumbs .crumb a"):
        name = anchor.get_text(" ", strip=True)
        href = anchor.get("href") or ""
        if name:
            crumbs.append({"name": name, "url": urljoin(page_url, href)})
    return crumbs


def extract_spec_icons(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Read the icon spec rows (Best for, Drop, Weight, and similar)."""
    specs = []
    seen: set[str] = set()
    for li in soup.find_all("li"):
        if li.find("use") is None:
            continue
        bold = li.find("span", class_="font-bold")
        if bold is None:
            continue
        label = re.sub(r"\s+", " ", bold.get_text(" ", strip=True)).strip()
        if not label.endswith(":"):
            continue
        label = label[:-1].strip()
        value = ""
        for span in li.find_all("span"):
            classes = span.get("class") or []
            if "font-bold" in classes:
                continue
            value = re.sub(r"\s+", " ", span.get_text(" ", strip=True)).strip()
            if value:
                break
        if not label or not value or len(label) > 40:
            continue
        key = _slug(label)
        if key in seen:
            continue
        seen.add(key)
        specs.append({"label": label, "value": value})
    return specs


def extract_powerreviews(html: str) -> dict[str, str] | None:
    """Read the PowerReviews widget config embedded for this product."""
    match = re.search(
        r"POWERREVIEWS\.display\.render\(\s*\{(?P<body>.*?)\bproduct\s*:",
        html,
        re.S,
    )
    if not match:
        return None
    body = match.group("body")

    def field(name: str) -> str | None:
        found = re.search(rf"{name}\s*:\s*['\"]([^'\"]+)['\"]", body)
        return found.group(1) if found else None

    api_key = field("api_key")
    locale = field("locale")
    merchant_id = field("merchant_id")
    page_id = field("page_id")
    if not all([api_key, locale, merchant_id, page_id]):
        return None
    return {
        "api_key": api_key,
        "locale": locale or "en_US",
        "merchant_id": merchant_id,
        "page_id": page_id,
    }


def shopify_js_url(page_url: str) -> str | None:
    """Same-product Shopify Ajax endpoint: /products/{handle}.js."""
    parsed = urlparse(page_url)
    parts = [part for part in parsed.path.split("/") if part]
    if "products" not in parts:
        return None
    index = parts.index("products")
    if index + 1 >= len(parts):
        return None
    handle = parts[index + 1]
    if handle.endswith(".js") or handle.endswith(".json"):
        handle = handle.rsplit(".", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}/products/{handle}.js"


def variant_query(page_url: str) -> str | None:
    values = parse_qs(urlparse(page_url).query).get("variant")
    if not values:
        return None
    value = values[0].strip()
    return value or None


def select_variant(product: dict[str, Any], variant_id: str | None) -> tuple[dict[str, Any] | None, str]:
    variants = product.get("variants") or []
    if variant_id:
        for variant in variants:
            if str(variant.get("id")) == variant_id:
                return variant, "query_param"
        return None, "query_param_not_found"
    for variant in variants:
        if variant.get("available"):
            return variant, "first_available"
    if variants:
        return variants[0], "first"
    return None, "none"


def option_names(product: dict[str, Any]) -> list[str]:
    names = []
    for option in product.get("options") or []:
        if isinstance(option, str):
            names.append(option)
        elif isinstance(option, dict) and option.get("name"):
            names.append(str(option["name"]))
    return names


def _option_map(variant: dict[str, Any], names: list[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    values = variant.get("options")
    if isinstance(values, list):
        for index, name in enumerate(names):
            if index < len(values) and values[index] not in (None, ""):
                mapped[name] = str(values[index])
        return mapped
    for index, name in enumerate(names):
        value = variant.get(f"option{index + 1}")
        if value not in (None, ""):
            mapped[name] = str(value)
    return mapped


def _image_src(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        src = value.get("src")
        if isinstance(src, str) and src:
            return src
    return None


def collect_images(product: dict[str, Any] | None, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    """Gallery and variant stills for this product. Video is skipped."""
    found: list[dict[str, Any]] = []
    if product:
        for media in product.get("media") or []:
            if media.get("media_type") not in (None, "image"):
                continue
            src = media.get("src") or (media.get("preview_image") or {}).get("src")
            if not src:
                continue
            url = absolute_https(src, page_url)
            if not allowed_image_url(url):
                continue
            found.append(
                {
                    "position": media.get("position") or len(found) + 1,
                    "src": url,
                    "alt": media.get("alt"),
                    "width": media.get("width"),
                    "height": media.get("height"),
                }
            )
        if not found:
            for index, image in enumerate(product.get("images") or [], start=1):
                src = image if isinstance(image, str) else _image_src(image)
                if not src:
                    continue
                url = absolute_https(src, page_url)
                if not allowed_image_url(url):
                    continue
                alt = image.get("alt") if isinstance(image, dict) else None
                found.append({"position": index, "src": url, "alt": alt, "width": None, "height": None})

    if not found:
        landing = soup.select_one("#landingImage, #imgBlkFront, #main-image")
        src = None
        if landing is not None:
            src = landing.get("data-old-hires") or landing.get("src")
        if isinstance(src, str):
            url = absolute_https(src, page_url)
            if allowed_image_url(url):
                found.append({"position": 1, "src": url, "alt": landing.get("alt"), "width": None, "height": None})
    if not found:
        for index, tag in enumerate(soup.find_all("meta", attrs={"property": "og:image"}), start=1):
            content = tag.get("content")
            if not isinstance(content, str):
                continue
            url = absolute_https(content, page_url)
            if allowed_image_url(url):
                found.append({"position": index, "src": url, "alt": None, "width": None, "height": None})
    if not found:
        for tag in soup.select("img[data-hires], meta[property='og:image']"):
            content = tag.get("data-hires") or tag.get("content")
            if not isinstance(content, str) or not content.startswith("http"):
                continue
            url = absolute_https(content, page_url)
            found.append(
                {
                    "position": len(found) + 1,
                    "src": url,
                    "alt": tag.get("alt"),
                    "width": None,
                    "height": None,
                }
            )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for image in found:
        key = urlparse(image["src"]).path
        if key in seen:
            continue
        seen.add(key)
        deduped.append(image)
    for index, image in enumerate(deduped, start=1):
        image["position"] = index
    return deduped


def _tag_attributes(tags: list[str]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for tag in tags:
        if ":" not in tag or tag.lower().startswith("breadcrumb"):
            continue
        label, value = tag.split(":", 1)
        key = _slug(label)
        if key and value.strip():
            attributes[key] = value.strip()
    return attributes


def _put_spec(specs: dict[str, str], key: str, value: str) -> None:
    if not key or not value:
        return
    previous = specs.get(key)
    if previous and previous != value and key == "drop" and re.search(r"\d\s*-\s*\d", previous):
        specs.setdefault("drop_range", previous)
    specs[key] = value


def _measurement_specs(points: list[str]) -> dict[str, str]:
    specs: dict[str, str] = {}
    for point in points:
        match = MEASUREMENT_RE.match(point.strip())
        if not match:
            continue
        _put_spec(specs, _slug(match.group("label")), match.group("value").strip())
    return specs


def _jsonld_nodes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            nodes.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    import json

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            walk(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return nodes


def _jsonld_product(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    product = None
    group = None
    for node in nodes:
        kinds = node.get("@type")
        values = kinds if isinstance(kinds, list) else [kinds]
        names = {str(kind).lower() for kind in values if isinstance(kind, str)}
        if "product" in names and product is None:
            product = node
        if "productgroup" in names and group is None:
            group = node
    return group or product


def _jsonld_variant(node: dict[str, Any] | None, page_url: str) -> dict[str, Any] | None:
    if not node:
        return None
    variants = node.get("hasVariant")
    if not isinstance(variants, list):
        return None
    token = urlparse(page_url).path.rstrip("/").split("/")[-1].lower()
    if token and token not in {"~", "html"}:
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            blob = " ".join(
                str(variant.get(key) or "")
                for key in ("sku", "mpn", "url", "@id", "name", "productID")
            ).lower()
            if token in blob:
                return variant
    return None


def _jsonld_gtin(node: dict[str, Any] | None) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in ("gtin", "gtin14", "gtin13", "gtin12", "gtin8"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _jsonld_price(node: dict[str, Any] | None, page_url: str) -> tuple[str | None, str | None]:
    sources: list[dict[str, Any]] = []
    variant = _jsonld_variant(node, page_url)
    if variant:
        sources.append(variant)
    if node:
        sources.append(node)
    for source in sources:
        offers = source.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        if not isinstance(offer, dict):
            continue
        price = offer.get("price")
        currency = offer.get("priceCurrency")
        if price is not None and str(price).strip():
            return str(price).strip(), currency if isinstance(currency, str) else None
    return None, None


def _jsonld_images(node: dict[str, Any] | None, page_url: str) -> list[dict[str, Any]]:
    if not node:
        return []
    urls: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
            urls.append(absolute_https(value, page_url))
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            add(value.get("url") or value.get("contentUrl") or value.get("image"))

    add(node.get("image"))
    variant = _jsonld_variant(node, page_url)
    if variant:
        add(variant.get("image"))
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        found.append({"position": len(found) + 1, "src": url, "alt": None, "width": None, "height": None})
        if len(found) == 8:
            break
    return found


def _availability_label(variant: dict[str, Any] | None) -> str | None:
    if variant is None:
        return None
    if variant.get("available") is True:
        return "in_stock"
    if variant.get("available") is False:
        return "out_of_stock"
    quantity = variant.get("inventory_quantity")
    if isinstance(quantity, int):
        return "in_stock" if quantity > 0 else "out_of_stock"
    return None


def _compact_variant(
    variant: dict[str, Any],
    names: list[str],
    currency: str | None,
    page_url: str,
) -> dict[str, Any]:
    image = _image_src(variant.get("featured_image"))
    return {
        "id": variant.get("id"),
        "title": variant.get("title"),
        "name": variant.get("name"),
        "sku": variant.get("sku") or None,
        "barcode": variant.get("barcode") or None,
        "price": cents_to_price(variant.get("price")),
        "compare_at_price": cents_to_price(variant.get("compare_at_price")),
        "currency": currency,
        "available": variant.get("available"),
        "availability": _availability_label(variant),
        "inventory_quantity": variant.get("inventory_quantity"),
        "options": _option_map(variant, names),
        "image": absolute_https(image, page_url) if image else None,
    }


def _parse_weight(display: str | None) -> dict[str, Any] | None:
    if not display:
        return None
    match = WEIGHT_RE.search(display)
    scope = None
    lowered = display.lower()
    if "per shoe" in lowered:
        scope = "per shoe"
    elif "per pair" in lowered:
        scope = "per pair"
    return {
        "display": display,
        "value": float(match.group("value")) if match else None,
        "unit": match.group("unit").lower() if match else None,
        "scope": scope,
    }


def _dimensions(specs: dict[str, str], attributes: dict[str, str], text: str) -> dict[str, Any]:
    drop = specs.get("heel_to_toe_drop") or specs.get("drop")
    length = width = height = None
    match = LWH_RE.search(text or "")
    if match:
        unit = (match.group("unit") or "").lower()
        suffix = f" {unit}" if unit else ""
        length = f"{match.group('l')}{suffix}"
        width = f"{match.group('w')}{suffix}"
        height = f"{match.group('h')}{suffix}"
    return {
        "heel_to_toe_drop": drop,
        "length": length,
        "width": width,
        "height": height,
        "widths_available": specs.get("widths"),
        "shoe_width": attributes.get("shoe_width"),
    }


def _ranking(tags: list[str], soup: BeautifulSoup) -> dict[str, Any] | None:
    designation = None
    for tag in tags:
        if re.fullmatch(r"basement\s*10", tag.strip(), re.I):
            designation = tag.strip()
            break
    flags = soup.select_one("#product-overview .product-flags")
    badge = flags.get_text(" ", strip=True) if flags else ""
    numeric = None
    if badge:
        found = re.search(r"#\s*(\d+)\b", badge)
        if found:
            numeric = int(found.group(1))
    if not designation and not badge and numeric is None:
        return None
    return {
        "designation": designation,
        "badge_text": badge or None,
        "numeric_rank": numeric,
        "source": "product tag" if designation else "page badge",
    }


def _ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


MAX_FEEDBACKS = 10
MAX_FEEDBACK_WORDS = 100


def _limit_words(text: Any, limit: int = MAX_FEEDBACK_WORDS) -> Any:
    if not isinstance(text, str):
        return text
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit])


def latest_feedbacks(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the 10 newest feedbacks and cap each comment at 100 words."""
    unique: list[dict[str, Any]] = []
    for review in reviews:
        comments = review.get("comments")
        if not isinstance(comments, str):
            continue
        key = " ".join(comments.split()).lower()
        if not _usable_comment(comments) or _same_feedback(review, key, unique):
            continue
        unique.append(review)
    ordered = sorted(unique, key=lambda review: review.get("created_at") or "", reverse=True)
    trimmed: list[dict[str, Any]] = []
    for review in ordered[:MAX_FEEDBACKS]:
        item = dict(review)
        item["comments"] = _limit_words(item.get("comments"))
        trimmed.append(item)
    return trimmed


def _same_feedback(review: dict[str, Any], key: str, kept: list[dict[str, Any]]) -> bool:
    """Drop a second copy of the same feedback from another part of the page."""
    created = str(review.get("created_at") or "")
    nickname = str(review.get("nickname") or "").strip().lower()
    for other in kept:
        other_key = " ".join(str(other.get("comments") or "").split()).lower()
        other_created = str(other.get("created_at") or "")
        other_name = str(other.get("nickname") or "").strip().lower()
        if created and other_created and created != other_created:
            continue
        if nickname and other_name and nickname != other_name:
            continue
        if key == other_key or (len(key) > 40 and (key in other_key or other_key in key)):
            return True
    return False


def _normalize_review(review: dict[str, Any]) -> dict[str, Any]:
    details = review.get("details") or {}
    metrics = review.get("metrics") or {}
    badges = review.get("badges") or {}
    media = []
    for item in review.get("media") or []:
        uri = item.get("uri") if isinstance(item, dict) else None
        if uri:
            media.append(absolute_https(uri, "https://res.cloudinary.com"))
    return {
        "id": review.get("review_id") or review.get("ugc_id"),
        "rating": metrics.get("rating"),
        "headline": details.get("headline"),
        "comments": details.get("comments"),
        "nickname": details.get("nickname"),
        "location": details.get("location"),
        "created_at": _ms_to_iso(details.get("created_date")),
        "bottom_line": details.get("bottom_line"),
        "source": details.get("source"),
        "brand_name": details.get("brand_name"),
        "product_page_id": details.get("product_page_id"),
        "properties": details.get("properties") or [],
        "verified_buyer": badges.get("is_verified_buyer"),
        "verified_reviewer": badges.get("is_verified_reviewer"),
        "helpful_votes": metrics.get("helpful_votes"),
        "not_helpful_votes": metrics.get("not_helpful_votes"),
        "media": media,
    }


def _highlighted(faceoff: Any) -> dict[str, Any] | None:
    if not isinstance(faceoff, dict):
        return None
    if not any(faceoff.get(key) for key in ("comments", "headline", "rating")):
        return None
    return {
        "id": faceoff.get("ugc_id"),
        "rating": faceoff.get("rating"),
        "headline": faceoff.get("headline"),
        "comments": faceoff.get("comments"),
    }


def _rating_block(rollup: dict[str, Any] | None, nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if rollup:
        histogram = rollup.get("rating_histogram") or []
        labeled = {}
        for index, count in enumerate(histogram, start=1):
            labeled[str(index)] = count
        return {
            "stars": rollup.get("average_rating"),
            "rating_count": rollup.get("rating_count"),
            "review_count": rollup.get("review_count"),
            "recommended_ratio": rollup.get("recommended_ratio"),
            "rating_histogram": labeled or None,
            "native_review_count": rollup.get("native_review_count"),
            "syndicated_review_count": rollup.get("syndicated_review_count"),
            "highlighted_positive": _highlighted(rollup.get("faceoff_positive")),
            "highlighted_negative": _highlighted(rollup.get("faceoff_negative")),
        }
    for node in nodes:
        aggregate = node.get("aggregateRating")
        if isinstance(aggregate, dict) and aggregate.get("ratingValue"):
            return {
                "stars": _number(aggregate.get("ratingValue")),
                "rating_count": _number(aggregate.get("ratingCount")),
                "review_count": _number(aggregate.get("reviewCount")),
                "recommended_ratio": None,
                "rating_histogram": None,
            }
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def _amazon_brand(soup: BeautifulSoup) -> str | None:
    node = soup.select_one("#bylineInfo")
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    text = re.sub(r"^visit the\s+", "", text, flags=re.I)
    text = re.sub(r"^brand:\s*", "", text, flags=re.I)
    text = re.sub(r"\s+store$", "", text, flags=re.I)
    return text.strip() or None


def _labeled_barcode(text: str) -> str | None:
    match = re.search(
        r"\b(?:UPC|EAN|GTIN)(?:\s*[-:]|\s+code)?\s*[:#]?\s*(\d{8}|\d{12}|\d{13}|\d{14})\b",
        text or "",
        re.I,
    )
    return match.group(1) if match else None


def _script_upc(soup: BeautifulSoup) -> str | None:
    """A page that reads an element and assigns it to a UPC field."""
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or not re.search(r"\bupc\b", text, re.I):
            continue
        match = re.search(r"""getElementById\(\s*["']([^"']+)["']\s*\)""", text)
        if not match:
            continue
        node = soup.find(id=match.group(1))
        if node is None:
            continue
        digits = re.sub(r"\D", "", node.get_text(" ", strip=True))
        if len(digits) in {8, 12, 13, 14}:
            return digits
    return None


def _itemprop_price(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    price = None
    for node in soup.find_all(attrs={"itemprop": "price"}):
        raw = node.get("content") or node.get_text(" ", strip=True)
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip().replace(",", "")
        if re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
            price = cleaned
            break
    currency = None
    node = soup.find(attrs={"itemprop": "priceCurrency"})
    if node is not None:
        raw = node.get("content") or node.get_text(" ", strip=True)
        if isinstance(raw, str) and raw.strip():
            currency = raw.strip()
    return price, currency


def _brand_from_jsonld(product: dict[str, Any] | None) -> str | None:
    if not product:
        return None
    brand = product.get("brand")
    if isinstance(brand, str):
        return brand
    if isinstance(brand, dict):
        name = brand.get("name")
        return str(name) if name else None
    return None


def assemble_product(
    page_url: str,
    html: str,
    product: dict[str, Any] | None,
    reviews_payload: dict[str, Any] | None,
    *,
    product_js_url: str | None,
    reviews_endpoint: str | None,
    extra_feedbacks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    nodes = _jsonld_nodes(soup)
    jsonld = _jsonld_product(nodes)
    product = product or {}
    tags = [str(tag) for tag in (product.get("tags") or [])]
    names = option_names(product)
    requested_variant = variant_query(page_url)
    selected, selection = select_variant(product, requested_variant) if product else (None, "none")

    description_html = product.get("description") or ""
    if not description_html and jsonld and isinstance(jsonld.get("description"), str):
        description_html = jsonld["description"]
    points = description_points(description_html)
    for item in soup.select("#feature-bullets li"):
        bullet = item.get_text(" ", strip=True)
        if bullet and bullet.lower() != "make sure this fits" and bullet not in points:
            points.append(bullet)
    text = html_to_text(description_html)
    attributes = _tag_attributes(tags)

    specs: dict[str, str] = {}
    for key, value in attributes.items():
        _put_spec(specs, key, value)
    for key, value in _measurement_specs(points).items():
        _put_spec(specs, key, value)
    icon_rows = extract_spec_icons(soup)
    for row in icon_rows:
        _put_spec(specs, _slug(row["label"]), row["value"])

    currency = _meta(soup, prop="og:price:currency") or _meta(soup, prop="product:price:currency")
    if not currency and jsonld:
        offers = jsonld.get("offers")
        if isinstance(offers, dict):
            currency = offers.get("priceCurrency")
        elif isinstance(offers, list) and offers and isinstance(offers[0], dict):
            currency = offers[0].get("priceCurrency")

    selected_record = (
        _compact_variant(selected, names, currency, page_url) if selected else None
    )
    variants = [
        _compact_variant(variant, names, currency, page_url)
        for variant in (product.get("variants") or [])
    ]

    price = selected_record["price"] if selected_record else cents_to_price(product.get("price"))
    compare = selected_record["compare_at_price"] if selected_record else cents_to_price(product.get("compare_at_price"))
    if price is None:
        price = _meta(soup, prop="og:price:amount") or _meta(soup, prop="product:price:amount")
    jsonld_price, jsonld_currency = _jsonld_price(jsonld, page_url)
    if price is None:
        price = jsonld_price
    itemprop_price, itemprop_currency = _itemprop_price(soup)
    if price is None:
        price = itemprop_price
    if not currency:
        currency = jsonld_currency or itemprop_currency

    title = product.get("title") or _meta(soup, prop="og:title")
    if not title and jsonld and isinstance(jsonld.get("name"), str):
        title = jsonld["name"].strip() or None
    if not title:
        product_title = soup.select_one("#productTitle")
        title = product_title.get_text(" ", strip=True) if product_title else None
    if not title:
        heading = soup.find(["h1", "h2"])
        title = heading.get_text(" ", strip=True) if heading else None
    brand = product.get("vendor") or _brand_from_jsonld(jsonld) or _amazon_brand(soup)
    if not brand:
        brand_node = soup.find(attrs={"itemprop": "brand"})
        if brand_node is not None:
            brand = brand_node.get_text(" ", strip=True) or None
    if not brand:
        vendor_link = soup.select_one("#product-overview a[href*='/collections/']")
        if vendor_link:
            brand = vendor_link.get_text(" ", strip=True) or None

    materials = [point for point in points if MATERIAL_RE.search(point)]
    features = [point for point in points if not MEASUREMENT_RE.match(point.strip())]
    weight = _parse_weight(specs.get("weight") or specs.get("weight_per_shoe"))
    dimensions = _dimensions(specs, attributes, text)
    ranking = _ranking(tags, soup)
    rollup = (reviews_payload or {}).get("rollup")
    rating = _rating_block(rollup if isinstance(rollup, dict) else None, nodes)
    reviews = latest_feedbacks(
        [
            _normalize_review(review)
            for review in ((reviews_payload or {}).get("reviews") or [])
            if isinstance(review, dict)
        ]
        + list(extra_feedbacks or [])
        + feedbacks_from_html(html)
    )

    on_sale = False
    if price and compare:
        try:
            on_sale = float(compare) > float(price)
        except ValueError:
            on_sale = False

    options = []
    for option in product.get("options") or []:
        if isinstance(option, dict):
            options.append(
                {
                    "name": option.get("name"),
                    "position": option.get("position"),
                    "values": option.get("values"),
                }
            )
        elif isinstance(option, str):
            options.append({"name": option, "values": None})

    images = collect_images(product or None, soup, page_url)
    if not images:
        images = _jsonld_images(jsonld, page_url)
    barcode = (selected_record or {}).get("barcode") if selected_record else None
    if not barcode:
        barcode = _jsonld_gtin(_jsonld_variant(jsonld, page_url)) or _jsonld_gtin(jsonld)
    if not barcode:
        barcode = _labeled_barcode(soup.get_text(" ", strip=True)) or _script_upc(soup)
    asin_node = soup.select_one("input#ASIN")
    asin = asin_node.get("value").strip() if asin_node and isinstance(asin_node.get("value"), str) else None
    breadcrumbs = extract_breadcrumbs(soup, page_url)

    return {
        "requested_url": page_url,
        "title": title,
        "brand": brand,
        "product_id": product.get("id") or (jsonld or {}).get("productID") or (jsonld or {}).get("sku") or asin,
        "handle": product.get("handle"),
        "product_type": product.get("type") or product.get("product_type"),
        "requested_variant_id": requested_variant,
        "variant_selection": selection,
        "selected_variant": selected_record,
        "price": price,
        "compare_at_price": compare,
        "currency": currency,
        "on_sale": on_sale,
        "availability": selected_record["availability"] if selected_record else (
            "in_stock" if product.get("available") is True else "out_of_stock" if product.get("available") is False else None
        ),
        "sku": (selected_record or {}).get("sku") if selected_record else (product.get("sku") or None),
        "barcode": barcode,
        "description": {
            "text": text or None,
            "html": description_html or None,
        },
        "features": features,
        "description_points": points,
        "dimensions": dimensions,
        "specs": specs,
        "spec_icons": icon_rows,
        "materials": materials,
        "weight": weight,
        "ranking": ranking,
        "popularity": None,
        "star_rating": None if rating is None else rating.get("stars"),
        "review_count": None if rating is None else rating.get("review_count"),
        "rating_count": None if rating is None else rating.get("rating_count"),
        "rating": rating,
        "reviews": reviews,
        "reviews_note": None if reviews else "No user feedback found on this page.",
        "breadcrumbs": breadcrumbs,
        "options": options,
        "attributes_from_tags": attributes,
        "tags": tags,
        "variants": variants,
        "images": images,
        "published_at": product.get("published_at"),
        "created_at": product.get("created_at"),
        "sources": {
            "product_page": page_url,
            "product_js": product_js_url,
            "reviews": reviews_endpoint,
        },
    }
