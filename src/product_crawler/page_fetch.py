"""Fetch a blocked product page before OpenAI runs.

httpx is the first attempt. When that page is a block screen, these steps
download HTML and the crawler scrapes the local file:

1. curl_cffi with a Chrome TLS fingerprint
2. a Wayback Machine copy
3. an archive.ph copy
4. Playwright
5. Scrapy
6. Crawl4AI, without an LLM

If those and the other-site search still fail, Exa live-crawls the URL.
BeautifulSoup parsing stays in extract.assemble_product. OpenAI runs after this.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import httpx

from product_crawler.blocked import blocked_note, cache_note, product_clues
from product_crawler.extract import assemble_product

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CACHE_METHODS = {"wayback", "archive.ph"}
Fetcher = Callable[[], tuple[str, str, str] | None]


def document_is_blocked(html: str) -> bool:
    sample = (html or "")[:8000].lower()
    if "<title>access denied</title>" in sample or ("access denied" in sample and "edgesuite" in sample):
        return True
    if "continue shopping" in sample and ("not a robot" in sample or "click the button below" in sample):
        return True
    return False


def shell_title(title: str | None) -> bool:
    text = (title or "").strip().lower()
    return text in {"skip to", "robot check", "access denied", "continue shopping"} or text.startswith("skip to ")


def page_has_product(html: str, url: str) -> bool:
    if document_is_blocked(html):
        return False
    title = assemble_product(
        url,
        html,
        None,
        None,
        product_js_url=None,
        reviews_endpoint=None,
    ).get("title")
    return bool(title) and not shell_title(title)


def archive_target(url: str) -> str:
    """Product path without ad query parameters, so a cache lookup can match."""
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.").split(".")[0] == "amazon":
        return urlunparse(parsed._replace(query="", fragment=""))
    return url.split("#", 1)[0]


def write_local_copy(cache_dir: Path, url: str, html: str, method: str, source_url: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    path = cache_dir / f"{digest}.html"
    path.write_text(html, encoding="utf-8")
    (cache_dir / f"{digest}.json").write_text(
        json.dumps({"url": url, "method": method, "source_url": source_url}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def recover_blocked_page(
    url: str,
    client: httpx.Client,
    *,
    cache_dir: Path | None = None,
    fetchers: list[Fetcher] | None = None,
) -> tuple[str, str, str | None, str] | None:
    """Return local HTML, source URL, note, and method when a later tool gets the product."""
    folder = cache_dir or Path("cache/pages")
    steps = fetchers if fetchers is not None else default_fetchers(client, url)
    for fetch in steps:
        try:
            found = fetch()
        except Exception:
            continue
        if found is None:
            continue
        html, source_url, method = found
        saved = _accept(folder, url, html, source_url, method)
        if saved is not None:
            return saved
    return None


def default_fetchers(client: httpx.Client, url: str) -> list[Fetcher]:
    return [
        lambda: _live(fetch_brightdata(client, url), url, "brightdata"),
        lambda: _live(fetch_curl_cffi(url), url, "curl_cffi"),
        lambda: _cached(fetch_wayback(client, url), "wayback"),
        lambda: _cached(fetch_archive_ph(client, url), "archive.ph"),
        lambda: _live(fetch_playwright(url), url, "playwright"),
        lambda: _live(fetch_scrapy(url), url, "scrapy"),
        lambda: _live(fetch_crawl4ai(url), url, "crawl4ai"),
    ]


def load_brightdata_key() -> str | None:
    key = os.environ.get("BRIGHTDATA_API_KEY")
    if key and key.strip():
        return key.strip()
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            key = payload.get("BRIGHTDATA_API_KEY") if isinstance(payload, dict) else None
            if isinstance(key, str) and key.strip():
                return key.strip()
    rtf = Path(__file__).resolve().parents[2] / "configs" / "brightdata.rtf"
    if rtf.is_file():
        match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rtf.read_text(encoding="utf-8", errors="replace"), re.I)
        if match:
            return match.group(0)
    return None


def fetch_brightdata(client: httpx.Client, url: str, *, timeout: float = 90) -> str | None:
    key = load_brightdata_key()
    if not key:
        return None
    zone = os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "cli_unlocker").strip() or "cli_unlocker"
    try:
        response = client.post(
            "https://api.brightdata.com/request",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"zone": zone, "url": url, "format": "raw"},
            timeout=timeout,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return response.text


def _trend_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[-+]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _trend_assessment(record: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"summary": "Insufficient observations to assess market direction.", "direction": "unknown", "confidence": "low", "model": "none"}
    if len(observations) < 2:
        return result
    api_key = load_api_key if False else None
    api_key = None
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        api_key = payload.get("OPENAI_API_KEY_AVATAR") if isinstance(payload, dict) else None
        if isinstance(api_key, str) and api_key.strip():
            api_key = api_key.strip()
            break
    if not api_key:
        return result
    prompt = "Assess this product market trend using only the observations below. Return JSON only: {\"summary\":\"\",\"direction\":\"rising|stable|falling|mixed|unknown\",\"confidence\":\"high|medium|low\"}.\nProduct: " + json.dumps({"title": record.get("title"), "brand": record.get("brand")}, ensure_ascii=False) + "\nObservations: " + json.dumps(observations[-12:], ensure_ascii=False)
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "gpt-4.1-mini", "input": prompt, "max_output_tokens": 500},
            timeout=120,
        )
        response.raise_for_status()
        text_parts: list[str] = []
        for output in response.json().get("output", []):
            for content in output.get("content", []) if isinstance(output, dict) else []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text_parts.append(str(content.get("text") or ""))
        parsed = json.loads("".join(text_parts).strip())
        direction = parsed.get("direction") if parsed.get("direction") in {"rising", "stable", "falling", "mixed", "unknown"} else "unknown"
        confidence = parsed.get("confidence") if parsed.get("confidence") in {"high", "medium", "low"} else "low"
        return {"summary": str(parsed.get("summary") or result["summary"]), "direction": direction, "confidence": confidence, "model": "gpt-4.1-mini"}
    except Exception:
        return result


def _record_trend_snapshot(record: dict[str, Any], section: dict[str, Any]) -> None:
    existing = section.get("trend_analysis") if isinstance(section.get("trend_analysis"), dict) else {}
    observations = existing.get("observations") if isinstance(existing.get("observations"), list) else []
    snapshot: dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
    price = _trend_number(record.get("price"))
    rating = _trend_number(record.get("star_rating"))
    review_count = _trend_number(record.get("review_count") or record.get("rating_count"))
    availability = record.get("availability")
    if price is not None:
        snapshot["price"] = price
    if rating is not None:
        snapshot["rating"] = rating
    if review_count is not None:
        snapshot["review_count"] = int(review_count)
    if isinstance(availability, str):
        lowered = availability.lower()
        snapshot["availability"] = 1 if any(word in lowered for word in ("in stock", "available")) else 0 if any(word in lowered for word in ("out of stock", "unavailable")) else None
    if len(observations) == 0:
        observations.append(snapshot)
    else:
        previous = observations[-1]
        changed = any(snapshot.get(key) != previous.get(key) for key in ("price", "rating", "review_count", "availability"))
        if changed:
            observations.append(snapshot)
    assessment = _trend_assessment(record, observations)
    section["trend_analysis"] = {"status": "completed" if len(observations) else "not_found", "currency": None, "observations": observations[-12:], "assessment": assessment}


def _brightdata_search(
    client: httpx.Client,
    query: str,
    *,
    limit: int = 5,
    timeout: float = 15,
) -> list[dict[str, Any]]:
    """Return direct, non-search-engine links and snippets for one Bright Data query."""
    from bs4 import BeautifulSoup

    url = "https://www.bing.com/search?" + httpx.QueryParams({"q": query}).__str__()
    response_html = fetch_brightdata(client, url, timeout=timeout)
    if not response_html:
        return []
    soup = BeautifulSoup(response_html, "html.parser")
    results: list[dict[str, Any]] = []
    for item in soup.select("li.b_algo")[:limit * 2]:
        link = item.select_one("h2 a[href]") or item.select_one("a[href]")
        if link is None:
            continue
        href = link.get("href") or ""
        title_text = link.get_text(" ", strip=True)
        if href.startswith("https://www.bing.com/ck/a?"):
            encoded = parse_qs(urlparse(href).query).get("u", [""])[0]
            if encoded.startswith("a1"):
                try:
                    href = base64.urlsafe_b64decode(encoded[2:] + "=" * (-len(encoded[2:]) % 4)).decode("utf-8", errors="replace")
                except Exception:
                    href = ""
        host = urlparse(href).netloc.lower()
        if not href.startswith(("http://", "https://")) or host in {"www.bing.com", "bing.com"}:
            continue
        snippet_node = item.select_one(".b_caption p") or item.select_one("p")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        result = {"url": href, "title": title_text[:300] or host, "snippet": snippet[:1000]}
        if result not in results:
            results.append(result)
        if len(results) >= limit:
            break
    return results


def enrich_product_folder_with_brightdata(folder: Path) -> dict[str, Any]:
    """Search for a generated product and add a separate Bright Data section to its JSON."""
    started_at = monotonic()
    total_budget = 30.0
    product_path = folder / "product.json"
    record = json.loads(product_path.read_text(encoding="utf-8"))

    identifiers: list[str] = []
    for key in ("barcode", "upc", "ean", "gtin", "asin", "sku", "title", "brand", "model"):
        value = record.get(key)
        if value is not None:
            text = str(value).strip()
            if text and text.lower() != "none" and text not in identifiers:
                identifiers.append(text)
    variant = record.get("variant")
    if isinstance(variant, dict):
        for key in ("barcode", "sku", "name"):
            text = str(variant.get(key) or "").strip()
            if text and text not in identifiers:
                identifiers.append(text)

    barcode = next((value for key, value in (("barcode", record.get("barcode")), ("upc", record.get("upc")), ("ean", record.get("ean")), ("gtin", record.get("gtin")), ("asin", record.get("asin"))) if value for value in [str(value).strip()] if value), None)
    brand = str(record.get("brand") or "").strip()
    title = str(record.get("title") or record.get("name") or "").strip()
    query = " ".join(part for part in (barcode, brand, title) if part)
    if not query and identifiers:
        query = " ".join(identifiers[:4])
    section: dict[str, Any] = {
        "status": "not_found",
        "query": query or None,
        "identifiers": identifiers,
        "ranking_evidence": {"status": "not_found", "items": []},
        "feedback": {"status": "not_found", "items": [], "rating": None, "review_count": None},
        "manuals_and_instructions": {"status": "not_found", "manuals": [], "instructions": []},
        "pricing": {"status": "not_found", "items": []},
        "identity_and_specifications": {"status": "not_found", "items": []},
        "retailer_sources": {"status": "not_found", "items": []},
        "sources": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "zone": os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "cli_unlocker"),
    }
    if not query:
        section["status"] = "no_identifier"
        _record_trend_snapshot(record, section)
        record["brightdata_crawler"] = section
        product_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return section

    research_queries = {
        "ranking_evidence": f'{query} ranking category popularity top 10 comparison ranking',
        "feedback": f'{query} customer reviews rating review count pros cons verified buyers',
        "manuals_and_instructions": f'{query} official manual PDF user guide instructions setup how to use support',
        "pricing": f'{query} current price availability retailer shipping',
        "identity_and_specifications": f'{query} specifications UPC EAN GTIN SKU model number dimensions warranty compatibility',
        "retailer_sources": f'{query} buy online retailer ecommerce product page',
    }
    client = httpx.Client(timeout=15)
    executor = ThreadPoolExecutor(max_workers=len(research_queries))
    searches = {
        field: executor.submit(_brightdata_search, client, research_query, limit=5, timeout=15)
        for field, research_query in research_queries.items()
    }
    remaining_budget = max(0.0, started_at + total_budget - monotonic() - 3.0)
    from concurrent.futures import wait

    completed, pending = wait(searches.values(), timeout=remaining_budget)
    for field, future in searches.items():
        error = None
        if future in pending:
            future.cancel()
            items = []
            error = f"Search stopped at the {total_budget:g}-second BrightData enrichment limit."
        else:
            try:
                items = future.result()
            except Exception as exc:
                items = []
                error = f"BrightData search failed: {type(exc).__name__}: {exc}"
        if field == "ranking_evidence":
            section[field] = {"status": "completed" if items else "not_found", "items": items, "note": "Source-backed ranking evidence only; no rank is inferred without an explicit source."}
        elif field == "feedback":
            section[field] = {"status": "completed" if items else "not_found", "items": items, "rating": None, "review_count": None}
        elif field == "manuals_and_instructions":
            manuals = [item for item in items if re.search(r"manual|guide|pdf|datasheet", " ".join((item["title"], item["snippet"])), re.I)]
            instructions = [item for item in items if re.search(r"how to|setup|install|instructions|support|use", " ".join((item["title"], item["snippet"])), re.I)]
            section[field] = {"status": "completed" if items else "not_found", "manuals": manuals, "instructions": instructions}
        elif field == "pricing":
            section[field] = {"status": "completed" if items else "not_found", "items": items}
        elif field == "identity_and_specifications":
            section[field] = {"status": "completed" if items else "not_found", "items": items}
        else:
            section[field] = {"status": "completed" if items else "not_found", "items": items}
        if error:
            section[field]["error"] = error
        section["sources"].extend(items)
        if items and section["status"] != "completed":
            section["status"] = "completed"
    executor.shutdown(wait=False, cancel_futures=True)
    client.close()

    _record_trend_snapshot(record, section)
    record["brightdata_crawler"] = section
    product_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return section


def fetch_curl_cffi(url: str) -> str | None:
    from curl_cffi import requests

    response = requests.get(url, impersonate="chrome", timeout=45, allow_redirects=True)
    if response.status_code >= 400:
        return None
    return response.text


def fetch_playwright(url: str) -> str | None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            return page.content()
        finally:
            browser.close()


def fetch_scrapy(url: str) -> str | None:
    script = '''
import sys
from scrapy import Spider
from scrapy.crawler import CrawlerProcess

class OnePage(Spider):
    name = "one-page"
    custom_settings = {
        "LOG_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "USER_AGENT": %r,
        "DOWNLOAD_TIMEOUT": 40,
    }

    def __init__(self, target, dest, **kwargs):
        super().__init__(**kwargs)
        self.start_urls = [target]
        self.dest = dest

    def parse(self, response):
        open(self.dest, "w", encoding="utf-8").write(response.text)

process = CrawlerProcess()
process.crawl(OnePage, target=sys.argv[1], dest=sys.argv[2])
process.start()
''' % USER_AGENT
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "page.html"
        script_path = Path(tmp) / "fetch.py"
        script_path.write_text(script, encoding="utf-8")
        try:
            completed = subprocess.run(
                [sys.executable, str(script_path), url, str(dest)],
                capture_output=True,
                timeout=70,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or not dest.is_file():
            return None
        return dest.read_text(encoding="utf-8", errors="replace")


def fetch_crawl4ai(url: str) -> str | None:
    import asyncio

    from crawl4ai import AsyncWebCrawler

    async def _run() -> str | None:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
        html = getattr(result, "html", None)
        return html if isinstance(html, str) and html.strip() else None

    return asyncio.run(_run())


def fetch_wayback(client: httpx.Client, url: str) -> tuple[str, str] | None:
    target = archive_target(url)
    try:
        listing = client.get(
            "https://web.archive.org/cdx/search/cdx",
            params={
                "url": target,
                "output": "json",
                "fl": "timestamp,original,statuscode",
                "filter": "statuscode:200",
                "limit": "20",
            },
        )
    except httpx.HTTPError:
        return None
    if listing.status_code >= 400:
        return None
    try:
        rows = listing.json()
    except json.JSONDecodeError:
        return None
    snapshots = [row for row in rows[1:] if isinstance(row, list) and len(row) >= 2]
    if not snapshots:
        return None
    timestamp, original = snapshots[-1][0], snapshots[-1][1]
    raw_url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    try:
        page = client.get(raw_url)
    except httpx.HTTPError:
        return None
    if page.status_code >= 400 or not page.text.strip():
        return None
    return page.text, raw_url


def fetch_archive_ph(client: httpx.Client, url: str) -> tuple[str, str] | None:
    target = archive_target(url)
    try:
        page = client.get(f"https://archive.ph/newest/{target}")
    except httpx.HTTPError:
        return None
    if page.status_code >= 400 or not page.text.strip():
        return None
    return page.text, str(page.url)


def _live(html: str | None, url: str, method: str) -> tuple[str, str, str] | None:
    if not html:
        return None
    return html, url, method


def _cached(found: tuple[str, str] | None, method: str) -> tuple[str, str, str] | None:
    if found is None:
        return None
    html, source_url = found
    return html, source_url, method


def _accept(
    cache_dir: Path,
    url: str,
    html: str,
    source_url: str,
    method: str,
) -> tuple[str, str, str | None, str] | None:
    if not html or not page_has_product(html, url):
        return None
    path = write_local_copy(cache_dir, url, html, method, source_url)
    local = path.read_text(encoding="utf-8")
    if not page_has_product(local, url):
        return None
    if method in CACHE_METHODS:
        note = cache_note(url, source_url)
    elif method == "exa" and _host(source_url) != _host(url):
        note = blocked_note(url, source_url)
    else:
        note = None
    return local, source_url, note, method


def load_exa_key() -> str | None:
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = payload.get("EXA_API_KEY") if isinstance(payload, dict) else None
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def fetch_exa(url: str) -> tuple[str, str, str] | None:
    """Live-crawl the product URL with Exa, then search if that page has no text."""
    key = load_exa_key()
    if not key:
        return None
    page = _exa_contents(key, archive_target(url))
    if page is None:
        page = _exa_search_page(key, url)
    if page is None:
        return None
    source = str(page.get("url") or url)
    return html_from_exa(page), source, "exa"


def html_from_exa(page: dict) -> str:
    """Turn Exa's text result into a local HTML page the product parser can read."""
    title = page.get("title") if isinstance(page.get("title"), str) else ""
    text = page.get("text") if isinstance(page.get("text"), str) else ""
    title = title.strip() or _first_line(text)
    paragraphs = "".join(f"<p>{html.escape(part.strip())}</p>" for part in text.split("\n") if part.strip())
    image = page.get("image") if isinstance(page.get("image"), str) else ""
    image_tag = ""
    if image.startswith("https://") and any(image.lower().split("?", 1)[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        image_tag = f'<meta property="og:image" content="{html.escape(image, quote=True)}" />'
    return (
        "<html><head>"
        f"<title>{html.escape(title)}</title>"
        f'<meta property="og:title" content="{html.escape(title, quote=True)}" />'
        f"{image_tag}</head><body><h1>{html.escape(title)}</h1>{paragraphs}</body></html>"
    )


def _exa_contents(key: str, url: str) -> dict | None:
    payload = _exa_post(
        key,
        "https://api.exa.ai/contents",
        {"urls": [url], "text": True, "maxAgeHours": 0, "livecrawlTimeout": 20000},
    )
    return _first_exa_page(payload)


def _exa_search_page(key: str, url: str) -> dict | None:
    payload = _exa_post(
        key,
        "https://api.exa.ai/search",
        {
            "query": _exa_query(url),
            "numResults": 5,
            "contents": {"text": True},
        },
    )
    requested = _host(url)
    for page in _exa_pages(payload):
        if _host(str(page.get("url") or "")) == requested:
            continue
        return page
    return None


def _exa_post(key: str, endpoint: str, body: dict) -> dict | None:
    try:
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=45,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _first_exa_page(payload: dict | None) -> dict | None:
    pages = _exa_pages(payload)
    return pages[0] if pages else None


def _exa_pages(payload: dict | None) -> list[dict]:
    if not payload:
        return []
    statuses = {
        item.get("id"): item.get("status")
        for item in payload.get("statuses") or []
        if isinstance(item, dict)
    }
    pages: list[dict] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        status = statuses.get(item.get("id")) or statuses.get(item.get("url"))
        if status not in (None, "success"):
            continue
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        source = item.get("url") if isinstance(item.get("url"), str) else ""
        if not source.startswith("http") or shell_title(title) or document_is_blocked(text):
            continue
        if len(text.split()) < 5 and not title.strip():
            continue
        pages.append(item)
    return pages


def _exa_query(url: str) -> str:
    path = unquote(urlparse(archive_target(url)).path)
    words = []
    for part in path.split("/"):
        if not part or part.lower() in {"dp", "gp", "product", "products"} or part.lower().startswith("ref="):
            continue
        words.append(part.replace("-", " "))
    return " ".join([*words[:6], *product_clues(url)])[:300] or url


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:180]
    return "Product"


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")
