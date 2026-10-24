"""Find user feedback on a product page, whatever widget the site uses.

The page itself is searched first: schema.org reviews, visible review or
feedback blocks, and review JSON embedded in the HTML. Known review feeds
named on that same page are returned as URLs for the crawler to request.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup, Tag

MIN_FEEDBACK_WORDS = 5

_BODY_SELECTORS = (
    "[itemprop=reviewBody]",
    "[itemprop=description]",
    ".review-content",
    ".review-body",
    ".review__body",
    ".review__content",
    ".feedback-body",
    ".feedback__body",
    ".comment-body",
    ".comment__body",
    ".yotpo-review-content",
    ".jdgm-rev__body",
    ".stamped-review-content",
    ".bv-content-summary-body-text",
)
_AUTHOR_SELECTORS = (
    "[itemprop=author]",
    ".author",
    ".reviewer",
    ".reviewer-name",
    ".yotpo-user-name",
    ".jdgm-rev__author",
    ".stamped-review-header-title",
)
_DATE_SELECTORS = (
    "time[datetime]",
    "[itemprop=datePublished]",
    ".review-date",
    ".feedback-date",
    ".comment-date",
    ".yotpo-review-date",
    ".jdgm-rev__timestamp",
    ".stamped-review-date",
)
_BLOCK_SELECTOR = ", ".join(
    (
        "[itemprop=review]",
        "[itemtype*='Review']",
        ".review",
        ".feedback",
        ".testimonial",
        ".comment",
        ".yotpo-review",
        ".jdgm-rev",
        ".stamped-review",
        ".bv-content-item",
        "[data-review-id]",
        "[class*='customer-review']",
        "[class*='customer-feedback']",
        "[class*='user-review']",
        "[class*='user-feedback']",
        "[class*='review-item']",
        "[class*='review-card']",
        "[class*='review__item']",
        "[class*='feedback-item']",
        "[class*='comment-item']",
    )
)
_SKIP_BLOCK = re.compile(
    r"review-form|write-a-review|comment-form|feedback-form|breadcrumb|related-product",
    re.I,
)
_PROMPT = re.compile(
    r"^(write a review|be the first|no reviews|customer reviews|reviews|please enter|enter your|your name|your email)\b",
    re.I,
)
_CHROME_PHRASE = re.compile(
    r"where did you see a lower price|found a lower price|please sign in to provide feedback|"
    r"how customer reviews and ratings work|fields with an asterisk|global ratings|out of 5 stars|"
    r"price availability|store name|please select province|date of the price|"
    r"did you find this product summary|thank you for your feedback|it is useful",
    re.I,
)
_CHROME_WORDS = {
    "star",
    "stars",
    "score",
    "rank",
    "review",
    "reviews",
    "rating",
    "ratings",
}
_REGION = re.compile(r"review|feedback|testimonial|comment|rating", re.I)
_HEADING = re.compile(
    r"\b(customer|user|product)?\s*(reviews?|feedback|testimonials?|comments?)\b",
    re.I,
)
_TEXT_KEYS = (
    "comments",
    "comment",
    "reviewBody",
    "review_body",
    "ReviewText",
    "reviewText",
    "review_text",
    "body",
    "content",
    "text",
    "feedback",
    "message",
    "review",
)
_AUTHOR_KEYS = (
    "nickname",
    "author",
    "reviewer",
    "display_name",
    "user_name",
    "customer_name",
    "name",
)
_DATE_KEYS = (
    "created_at",
    "created_date",
    "datePublished",
    "date",
    "published_at",
    "createdAt",
    "timestamp",
    "submissionTime",
)
_TITLE_KEYS = ("headline", "title", "reviewTitle")
_RATING_KEYS = ("rating", "score", "stars", "ratingValue")


def feedbacks_from_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[dict[str, Any]] = []
    found.extend(_from_jsonld(soup))
    found.extend(_from_blocks(soup))
    found.extend(_from_regions(soup))
    found.extend(_from_embedded_json(soup))
    return [item for item in found if item.get("comments")]


def review_feed_urls(html: str) -> list[tuple[str, str]]:
    """Return (kind, url) for review feeds this page names."""
    soup = BeautifulSoup(html or "", "html.parser")
    feeds: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, url: str) -> None:
        if url not in seen:
            seen.add(url)
            feeds.append((kind, url))

    for node in soup.select("[data-appkey], [data-app-key], [class*=yotpo]"):
        app = node.get("data-appkey") or node.get("data-app-key")
        product_id = node.get("data-product-id") or node.get("data-yotpo-product-id")
        if app and product_id:
            add(
                "yotpo",
                "https://api.yotpo.com/v1/widget/"
                f"{quote(str(app), safe='')}/products/{quote(str(product_id), safe='')}"
                "/reviews.json?per_page=20&sort=date&direction=desc",
            )
            break

    for node in soup.select(".jdgm-widget, [class*=jdgm], [data-shop-domain]"):
        product_id = node.get("data-id") or node.get("data-product-id")
        shop = node.get("data-shop-domain")
        if product_id and shop:
            add(
                "judgeme",
                "https://judge.me/api/v1/widgets/product_review"
                f"?shop_domain={quote(str(shop), safe='')}&product_id={quote(str(product_id), safe='')}"
                "&per_page=20",
            )
            break

    stamped = soup.select_one("#stamped-main-widget, .stamped-main-widget, [id*=stamped]")
    if stamped is not None:
        product_id = stamped.get("data-product-id")
        store = stamped.get("data-url") or _page_host(soup)
        api_key = stamped.get("data-api-key")
        if product_id and store:
            query = f"productId={quote(str(product_id), safe='')}&storeUrl={quote(str(store), safe='')}&take=20&page=1"
            if api_key:
                query += f"&apiKey={quote(str(api_key), safe='')}"
            add("stamped", f"https://stamped.io/api/widget/reviews?{query}")

    passkey = _script_value(html, "passkey")
    bv_product = None
    for node in soup.select("[data-bv-product-id], [data-product-id]"):
        if node.get("data-bv-product-id"):
            bv_product = node.get("data-bv-product-id")
            break
    if passkey and bv_product:
        add(
            "bazaarvoice",
            "https://api.bazaarvoice.com/data/reviews.json?apiversion=5.4"
            f"&passkey={quote(passkey, safe='')}&Filter=ProductId:{quote(bv_product, safe='')}"
            "&Sort=SubmissionTime:desc&Limit=20",
        )

    for node in soup.select("[data-oke-reviews-product-id], [data-oke-subscriber-id]"):
        product_id = node.get("data-oke-reviews-product-id")
        store = node.get("data-oke-subscriber-id") or _script_value(html, "subscriberId")
        if product_id and store:
            add(
                "okendo",
                "https://api.okendo.io/v1/stores/"
                f"{quote(str(store), safe='')}/products/{quote(str(product_id), safe='')}/reviews?limit=20",
            )
            break
    return feeds


def parse_review_feed(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    _walk_objects(payload, found)
    return found


def _from_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    import json

    found: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _jsonld_reviews(payload):
            item = _from_schema_review(node)
            if item:
                found.append(item)
    return found


def _jsonld_reviews(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        if _is_review_type(node.get("@type")):
            found.append(node)
        review = node.get("review")
        if review is not None:
            walk(review)
        for key, child in node.items():
            if key != "review":
                walk(child)

    walk(value)
    return found


def _is_review_type(kind: Any) -> bool:
    values = kind if isinstance(kind, list) else [kind]
    for value in values:
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if lowered == "review" or lowered.endswith("review"):
            return True
    return False


def _from_schema_review(node: dict[str, Any]) -> dict[str, Any] | None:
    comments = node.get("reviewBody") or node.get("description")
    if not isinstance(comments, str):
        return None
    rating = node.get("reviewRating")
    rating_value = rating.get("ratingValue") if isinstance(rating, dict) else rating
    author = node.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    return _feedback(
        comments=comments,
        headline=node.get("name") if isinstance(node.get("name"), str) else None,
        nickname=author if isinstance(author, str) else None,
        created_at=_to_iso(node.get("datePublished")),
        rating=_number(rating_value),
        source="json-ld",
    )


def _from_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for node in soup.select(_BLOCK_SELECTOR):
        if not isinstance(node, Tag):
            continue
        if node.find_parent(["script", "style", "form", "nav"]) is not None:
            continue
        identity = " ".join(node.get("class") or []) + " " + str(node.get("id") or "")
        if _SKIP_BLOCK.search(identity):
            continue
        if node.select(_BLOCK_SELECTOR):
            continue
        comments = _block_text(node)
        if comments is None:
            continue
        found.append(
            _feedback(
                comments=comments,
                headline=_first_text(node, ("h2", "h3", "h4", "[itemprop=name]")),
                nickname=_first_text(node, _AUTHOR_SELECTORS),
                created_at=_block_date(node),
                rating=_block_rating(node),
                source="page",
            )
        )
    return found


def _from_regions(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Read repeated cards under a feedback heading or a feedback container."""
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    regions: list[Tag] = []
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        label = heading.get_text(" ", strip=True)
        if not label or len(label.split()) > 8 or not _HEADING.search(label):
            continue
        region = heading.find_next_sibling()
        if isinstance(region, Tag):
            regions.append(region)
    for node in soup.find_all(True):
        if not isinstance(node, Tag):
            continue
        identity = _identity(node)
        label = str(node.get("aria-label") or "")
        if _REGION.search(identity) or _HEADING.search(label):
            if not _SKIP_BLOCK.search(identity):
                regions.append(node)
    for region in regions:
        cards = [
            child
            for child in region.find_all(recursive=False)
            if isinstance(child, Tag) and child.name not in ("script", "style", "form", "nav")
        ]
        usable = [card for card in cards if _card_comment(card)]
        if len(usable) < 2 and not any(_has_feedback_signal(card) for card in usable):
            continue
        for card in usable:
            if id(card) in seen or _inside_known_block(card):
                continue
            seen.add(id(card))
            comments = _card_comment(card)
            if comments is None:
                continue
            found.append(
                _feedback(
                    comments=comments,
                    headline=_first_text(card, ("h2", "h3", "h4", "[itemprop=name]")),
                    nickname=_first_text(card, _AUTHOR_SELECTORS),
                    created_at=_block_date(card),
                    rating=_block_rating(card),
                    source="page",
                )
            )
    return found


def _card_comment(node: Tag) -> str | None:
    comments = _block_text(node)
    if comments is None or len(comments.split()) > 400:
        return None
    return comments


def _has_feedback_signal(node: Tag) -> bool:
    return _block_date(node) is not None or _block_rating(node) is not None or (
        _first_text(node, _AUTHOR_SELECTORS) is not None
    )


def _inside_known_block(node: Tag) -> bool:
    parent = node.parent
    while isinstance(parent, Tag):
        classes = " ".join(parent.get("class") or [])
        if parent.get("itemprop") == "review" or _SKIP_BLOCK.search(classes):
            return True
        if _matches_block(parent):
            return True
        parent = parent.parent
    return False


def _matches_block(node: Tag) -> bool:
    classes = node.get("class") or []
    if node.get("itemprop") == "review" or node.get("data-review-id"):
        return True
    itemtype = str(node.get("itemtype") or "")
    if "Review" in itemtype:
        return True
    for name in classes:
        if name in {
            "review",
            "feedback",
            "testimonial",
            "comment",
            "yotpo-review",
            "jdgm-rev",
            "stamped-review",
            "bv-content-item",
        }:
            return True
        lowered = name.lower()
        if any(
            token in lowered
            for token in (
                "customer-review",
                "customer-feedback",
                "user-review",
                "user-feedback",
                "review-item",
                "review-card",
                "feedback-item",
                "comment-item",
            )
        ):
            return True
    return False


def _identity(node: Tag) -> str:
    return " ".join(node.get("class") or []) + " " + str(node.get("id") or "")


def _from_embedded_json(soup: BeautifulSoup) -> list[dict[str, Any]]:
    import json

    found: list[dict[str, Any]] = []
    for script in soup.find_all("script"):
        if script.get("type") == "application/ld+json":
            continue
        raw = script.string or ""
        if "review" not in raw.lower() and "feedback" not in raw.lower():
            continue
        for candidate in _json_chunks(raw):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            _walk_objects(payload, found, source="page-json")
    return found


def _walk_objects(value: Any, found: list[dict[str, Any]], source: str = "feed") -> None:
    if isinstance(value, list):
        for child in value:
            _walk_objects(child, found, source)
        return
    if not isinstance(value, dict):
        return
    item = _from_generic(value, source)
    if item is not None:
        found.append(item)
    for child in value.values():
        if isinstance(child, (dict, list)):
            _walk_objects(child, found, source)


def _from_generic(node: dict[str, Any], source: str) -> dict[str, Any] | None:
    comments = _first_string(node, _TEXT_KEYS)
    if comments is None or len(comments.split()) < MIN_FEEDBACK_WORDS:
        return None
    author = _person_name(node)
    created = None
    for key in _DATE_KEYS:
        if key in node:
            created = _to_iso(node.get(key))
            if created:
                break
    rating = None
    for key in _RATING_KEYS:
        if key in node:
            rating = _number(node.get(key))
            if rating is not None:
                break
    if author is None and created is None and rating is None:
        return None
    headline = _first_string(node, _TITLE_KEYS)
    if headline == comments:
        headline = None
    return _feedback(
        comments=comments,
        headline=headline,
        nickname=author,
        created_at=created,
        rating=rating,
        source=source,
        review_id=node.get("id") or node.get("review_id"),
    )


def _feedback(
    *,
    comments: str,
    headline: str | None,
    nickname: str | None,
    created_at: str | None,
    rating: float | None,
    source: str,
    review_id: Any = None,
) -> dict[str, Any]:
    text = " ".join(str(comments).split())
    return {
        "id": review_id,
        "rating": rating,
        "headline": headline.strip() if isinstance(headline, str) and headline.strip() else None,
        "comments": text,
        "nickname": nickname.strip() if isinstance(nickname, str) and nickname.strip() else None,
        "location": None,
        "created_at": created_at,
        "bottom_line": None,
        "source": source,
        "brand_name": None,
        "product_page_id": None,
        "properties": [],
        "verified_buyer": None,
        "verified_reviewer": None,
        "helpful_votes": None,
        "not_helpful_votes": None,
        "media": [],
    }


def _block_text(node: Tag) -> str | None:
    for selector in _BODY_SELECTORS:
        found = node.select_one(selector)
        if found is None or found is node:
            continue
        text = found.get_text(" ", strip=True)
        if _usable_comment(text):
            return text
    text = node.get_text(" ", strip=True)
    if _usable_comment(text) and len(text.split()) <= 400:
        return text
    return None


def _usable_comment(text: str) -> bool:
    words = text.split()
    if len(words) < MIN_FEEDBACK_WORDS:
        return False
    if _PROMPT.search(text.strip()) or _CHROME_PHRASE.search(text):
        return False
    if text.count("|") >= 3 or text.lower().count("star") >= 3 or text.count("%") >= 3:
        return False
    plain = [re.sub(r"[^\w]+", "", word).lower() for word in words]
    plain = [word for word in plain if word]
    if plain and sum(word in _CHROME_WORDS for word in plain) >= len(plain) * 0.6:
        return False
    return True


def _first_text(node: Tag, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        found = node.select_one(selector)
        if found is None:
            continue
        text = found.get_text(" ", strip=True)
        if text and len(text.split()) <= 12:
            return text
    return None


def _block_date(node: Tag) -> str | None:
    for selector in _DATE_SELECTORS:
        found = node.select_one(selector)
        if found is None:
            continue
        raw = found.get("datetime") or found.get("content") or found.get_text(" ", strip=True)
        parsed = _to_iso(raw)
        if parsed:
            return parsed
    return None


def _block_rating(node: Tag) -> float | None:
    rated = node.select_one("[itemprop=ratingValue]")
    if rated is not None:
        value = _number(rated.get("content") or rated.get_text(" ", strip=True))
        if value is not None:
            return value
    labeled = node.select_one("[aria-label*='star' i]")
    if labeled is not None:
        match = re.search(r"(\d+(?:\.\d+)?)", labeled.get("aria-label") or "")
        if match:
            return _number(match.group(1))
    return None


def _person_name(node: dict[str, Any]) -> str | None:
    for key in _AUTHOR_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip() and len(value.split()) <= 8:
            return value.strip()
        if isinstance(value, dict):
            name = value.get("display_name") or value.get("name") or value.get("nickname")
            if isinstance(name, str) and name.strip():
                return name.strip()
    user = node.get("user")
    if isinstance(user, dict):
        name = user.get("display_name") or user.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _first_string(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if match:
            return float(match.group(0))
    return None


def _to_iso(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _page_host(soup: BeautifulSoup) -> str | None:
    canonical = soup.find("link", rel="canonical")
    href = canonical.get("href") if canonical is not None else None
    if not isinstance(href, str) or not href:
        return None
    parsed = urlparse(href)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _script_value(html: str, name: str) -> str | None:
    match = re.search(rf"{name}\s*[:=]\s*['\"]([^'\"]+)['\"]", html, re.I)
    return match.group(1) if match else None


def _json_chunks(raw: str) -> list[str]:
    chunks: list[str] = []
    start = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "[{":
            if depth == 0:
                start = index
            depth += 1
        elif char in "]}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    chunks.append(raw[start : index + 1])
                    start = None
    return chunks
