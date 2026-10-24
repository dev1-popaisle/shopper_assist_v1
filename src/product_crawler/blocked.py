"""When a product page blocks the crawler, find the same product elsewhere.

The clues come from the URL path in urls.txt: a UPC, EAN, GTIN, Amazon ASIN,
or style code. Another site's page is used only when it prints that same code. The
original URL stays the source, and the product page says it was not used
because the site blocked the crawler.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

_BARCODE_LENGTHS = {8, 12, 13, 14}
_SKIP_HOSTS = {
    "duckduckgo.com",
    "google.com",
    "bing.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "instagram.com",
    "pinterest.com",
    "reddit.com",
}
_STYLE_CODE = re.compile(r"[A-Za-z]{1,6}\d{3,}[A-Za-z]?")
_ASIN = re.compile(r"B0[A-Z0-9]{8}")


def product_clues(url: str) -> list[str]:
    """Barcode, ASIN, and style codes in the blocked product path."""
    text = unquote(urlparse(url).path)
    clues: list[str] = []
    for digits in re.findall(r"\d{8,14}", text):
        if len(digits) in _BARCODE_LENGTHS and digits not in clues:
            clues.append(digits)
    for match in _ASIN.finditer(text.upper()):
        code = match.group(0)
        if code not in clues:
            clues.append(code)
    for match in _STYLE_CODE.finditer(text):
        code = match.group(0)
        if code not in clues and not code.isdigit():
            clues.append(code)
    return clues


def search_result_urls(page_html: str) -> list[str]:
    """Product links from a DuckDuckGo HTML results page."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r'href="([^"]+)"', page_html):
        target = _result_target(html.unescape(raw))
        if target is None or target in seen or not _usable_host(target):
            continue
        seen.add(target)
        found.append(target)
    return found


def rank_candidates(urls: list[str], clues: list[str], blocked_url: str) -> list[str]:
    """Prefer pages whose address already contains this product's barcode."""
    codes = [code for code in clues if _barcode([code]) or not code.isdigit()]
    ranked: list[tuple[int, str]] = []
    for url in urls:
        if _same_site(url, blocked_url):
            continue
        address = unquote(url)
        if any(code.lower() in address.lower() for code in codes):
            ranked.append((0, url))
        else:
            ranked.append((1, url))
    ranked.sort(key=lambda item: item[0])
    return [url for _rank, url in ranked]


def page_matches(page_html: str, clues: list[str]) -> bool:
    """True when the other page prints this product's barcode, or its style code."""
    barcode = _barcode(clues)
    if barcode:
        return barcode in page_html
    return any(clue.lower() in page_html.lower() for clue in clues if not clue.isdigit())


def blocked_note(requested_url: str, fetched_url: str) -> str:
    return (
        "The original URL in urls.txt was not used because that site blocked the crawler. "
        f"This product was read from {fetched_url} instead of {requested_url}."
    )


def cache_note(requested_url: str, archive_url: str) -> str:
    return (
        "The live page blocked the crawler. A cached copy of the original URL was downloaded "
        f"and scraped locally from {archive_url} instead of reading {requested_url} live."
    )


def _result_target(href: str) -> str | None:
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("http"):
        return None
    parsed = urlparse(href)
    values = parse_qs(parsed.query).get("uddg") or []
    if values:
        target = unquote(values[0])
        return target if target.startswith("http") else None
    if _host(href) == "duckduckgo.com":
        return None
    return href.split("#", 1)[0]


def _usable_host(url: str) -> bool:
    host = _host(url)
    return bool(host) and host not in _SKIP_HOSTS and not any(host.endswith("." + skipped) for skipped in _SKIP_HOSTS)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _same_site(url: str, blocked_url: str) -> bool:
    host = _host(url)
    blocked = _host(blocked_url)
    if host == blocked:
        return True
    # Regional Amazon storefronts share the same block page.
    return host.split(".")[0] == blocked.split(".")[0] == "amazon"


def _barcode(clues: list[str]) -> str | None:
    for clue in clues:
        if clue.isdigit() and len(clue) in _BARCODE_LENGTHS:
            return clue
    return None
