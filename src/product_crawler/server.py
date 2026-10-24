"""Local product pages.

Each scraped UUID is a page at http://127.0.0.1:8888/<uuid>. The page shows
one image, the product name, and the market barcode stored in DuckDB.
"""

from __future__ import annotations

import html
import base64
import hashlib
import hmac
import json
import re
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from product_crawler.crawl import crawl
from product_crawler.jev import ask_jev, load_openrouter_key
from product_crawler.jev_insight import explain_decision
from product_crawler.prompt_search import create_from_prompt
from product_crawler.url_file import UrlFileError, add_url, crawl_pending, remove_folder
from product_crawler.store import (
    UUID_RE,
    ProductStore,
    default_db_path,
    default_output_root,
)

HOST = "127.0.0.1"
PORT = 8888
_RUN_LOCK = threading.Lock()
_SESSION_SECRET = secrets.token_bytes(32)
_SESSION_COOKIE = "product_search_session"
_SESSION_MAX_AGE = 8 * 60 * 60


class ProductCreate(BaseModel):
    payload: dict[str, Any] | None = None
    output_id: str | None = None


class ProductUpdate(BaseModel):
    name: str | None = None
    brand: str | None = None
    barcode: str | None = Field(default=None)


class JevQuestion(BaseModel):
    ids: list[str]
    question: str
    use_falkordb: bool = False


class UrlAdd(BaseModel):
    url: str
    brightdata: bool = False


def create_app(
    db_path: Path | None = None,
    output_root: Path | None = None,
    urls_path: Path | None = None,
    accounts_path: Path | None = None,
) -> FastAPI:
    store = ProductStore(db_path or default_db_path())
    root = output_root or default_output_root()
    urls_file = urls_path or _default_urls_path()
    accounts_file = accounts_path or _default_accounts_path()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.init()
        store.import_missing(root)
        yield

    app = FastAPI(title="Product pages", lifespan=lifespan)
    app.state.store = store
    app.state.output_root = root
    app.state.urls_path = urls_file
    app.state.accounts_path = accounts_file

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if request.url.path == "/login":
            return await call_next(request)
        if _session_username(request.cookies.get(_SESSION_COOKIE), accounts_file) is not None:
            return await call_next(request)
        if request.method != "GET" or request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Login required."}, status_code=401)
        next_path = request.url.path
        if request.url.query:
            next_path += "?" + request.url.query
        return RedirectResponse("/login?next=" + quote(next_path, safe="/?:=&"), status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(next: str = "/") -> HTMLResponse:
        response = HTMLResponse(_login_html(next=_safe_next(next)))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/login")
    async def login(request: Request):
        raw = (await request.body()).decode("utf-8", errors="replace")
        values = parse_qs(raw)
        username = (values.get("username") or [""])[0].strip().casefold()
        password = (values.get("password") or [""])[0]
        next_path = _safe_next((values.get("next") or ["/"])[0])
        accounts = _load_accounts(accounts_file)
        stored_password = accounts.get(username)
        if stored_password is None or not hmac.compare_digest(
            stored_password.encode("utf-8"), password.encode("utf-8")
        ):
            response = HTMLResponse(
                _login_html(next=next_path, error="Invalid username or password."),
                status_code=401,
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            _SESSION_COOKIE,
            _make_session(username),
            max_age=_SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/logout")
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(_SESSION_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, background_tasks: BackgroundTasks, search_error: str | None = None) -> str:
        products = store.list_products()
        username = _session_username(request.cookies.get(_SESSION_COOKIE), accounts_file)
        if username:
            background_tasks.add_task(_backfill_result_metadata, username, store.db_path, root)
        return _page("Product Search Results : ", _index_html(products, search_error=search_error))

    @app.post("/search")
    async def search_prompt(request: Request, background_tasks: BackgroundTasks) -> RedirectResponse:
        raw = (await request.body()).decode("utf-8", errors="replace")
        values = parse_qs(raw)
        prompt = (values.get("prompt") or [""])[0].strip()
        use_brightdata = (values.get("brightdata") or ["false"])[0].lower() == "true"
        username = _session_username(request.cookies.get(_SESSION_COOKIE), accounts_file)
        try:
            folder = create_from_prompt(prompt, root)
            if use_brightdata:
                from product_crawler.page_fetch import enrich_product_folder_with_brightdata
                enrich_product_folder_with_brightdata(folder)
            _stamp_product_result(folder, username or "unknown", "openai_brightdata" if use_brightdata else "openai_api")
            row = store.import_folder(folder)
        except Exception:
            return RedirectResponse("/?search_error=1", status_code=303)
        background_tasks.add_task(_sync_falkor_product, row["id"], store.db_path, root)
        return RedirectResponse("/", status_code=303)

    @app.post("/urls/add")
    def add_product_url(body: UrlAdd) -> dict[str, str]:
        if not _RUN_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A crawl is already running.")
        try:
            try:
                status = add_url(urls_file, body.url, output_root=root, brightdata=body.brightdata)
            except UrlFileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"status": status, "url": body.url.strip()}
        finally:
            _RUN_LOCK.release()

    @app.post("/urls/run")
    def run_urls(request: Request, background_tasks: BackgroundTasks, brightdata: bool = False) -> dict[str, Any]:
        if not _RUN_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A crawl is already running.")
        try:
            if not urls_file.is_file():
                raise HTTPException(status_code=404, detail="urls.txt was not found.")
            try:
                folders, errors = crawl_pending(urls_file, lambda url: crawl(url, root))
            except UrlFileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            added: list[dict[str, str]] = []
            synced_ids: list[str] = []
            username = _session_username(request.cookies.get(_SESSION_COOKIE), accounts_file) or "unknown"
            for folder in folders:
                try:
                    if brightdata:
                        from product_crawler.page_fetch import enrich_product_folder_with_brightdata
                        enrich_product_folder_with_brightdata(folder)
                    _stamp_product_result(folder, username, "base_crawler_brightdata" if brightdata else "base_crawler")
                    row = store.import_folder(folder)
                except Exception as exc:
                    errors.append(f"{folder.name}: saved the page but did not store it: {exc}")
                    continue
                added.append({
                    "id": row["id"],
                    "name": row["name"] or row["id"],
                    "brightdata_enriched": bool(brightdata),
                    "openai_result": False,
                })
                synced_ids.append(row["id"])
            if synced_ids:
                background_tasks.add_task(_sync_falkor_products, synced_ids, store.db_path, root)
            return {"added": added, "errors": errors}
        finally:
            _RUN_LOCK.release()

    @app.post("/jev/decide")
    def jev_decide(body: JevQuestion) -> dict[str, Any]:
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="Enter a question.")
        if len(question) > 2000:
            raise HTTPException(status_code=400, detail="Ask the question in 2000 characters or less.")
        if not body.ids:
            raise HTTPException(status_code=400, detail="Check at least one product.")
        api_key = load_openrouter_key()
        if not api_key:
            raise HTTPException(status_code=400, detail="OPENROUTER_API_KEY is not in api_config.json")
        products: list[dict[str, Any]] = []
        for product_id in body.ids:
            _require_uuid(product_id)
            row = store.get(product_id)
            if row is None:
                raise HTTPException(status_code=404, detail="product not found")
            products.append(row)
        falkordb_analysis = None
        if body.use_falkordb:
            try:
                from product_crawler.jev_falkor import pricing_review_analysis

                falkordb_analysis = pricing_review_analysis(body.ids)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail="FalkorDB pricing analysis is unavailable. Sync the selected products and check the graph connection.",
                ) from exc
        try:
            decision = ask_jev(products, question, api_key, falkordb_analysis=falkordb_analysis)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if falkordb_analysis is not None:
            decision["falkordb_analysis"] = falkordb_analysis
        try:
            decision["insight"] = explain_decision(question, decision)
        except RuntimeError as exc:
            decision["insight"] = str(exc)
        return decision

    @app.get("/api/products")
    def list_products() -> list[dict[str, Any]]:
        return store.list_products()

    @app.get("/api/products/{product_id}")
    def read_product(product_id: str) -> dict[str, Any]:
        _require_uuid(product_id)
        row = store.get(product_id)
        if row is None:
            raise HTTPException(status_code=404, detail="product not found")
        return row

    @app.post("/api/products", status_code=201)
    def create_product(request: Request, body: ProductCreate, background_tasks: BackgroundTasks) -> dict[str, Any]:
        username = _session_username(request.cookies.get(_SESSION_COOKIE), accounts_file) or "unknown"
        if body.output_id:
            _require_uuid(body.output_id)
            folder = root / body.output_id
            if not (folder / "product.json").is_file():
                raise HTTPException(status_code=404, detail="output folder not found")
            if store.get(body.output_id) is not None:
                raise HTTPException(status_code=409, detail="product already exists")
            _stamp_product_result(folder, username, _infer_search_method(json.loads((folder / "product.json").read_text(encoding="utf-8"))))
            row = store.import_folder(folder)
            background_tasks.add_task(_sync_falkor_product, row["id"], store.db_path, root)
            return row
        if not isinstance(body.payload, dict):
            raise HTTPException(status_code=400, detail="payload or output_id is required")
        try:
            record = dict(body.payload)
            record.setdefault("owner_username", username)
            record.setdefault("search_generated_at", datetime.now(timezone.utc).isoformat())
            record.setdefault("search_method", _infer_search_method(record))
            row = store.create(record)
            background_tasks.add_task(_sync_falkor_product, row["id"], store.db_path, root)
            return row
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="product already exists") from exc

    @app.put("/api/products/{product_id}")
    def update_product(product_id: str, body: ProductUpdate, background_tasks: BackgroundTasks) -> dict[str, Any]:
        _require_uuid(product_id)
        if body.name is None and body.brand is None and body.barcode is None:
            raise HTTPException(status_code=400, detail="name, brand, or barcode is required")
        updated = store.update(
            product_id,
            name=body.name,
            brand=body.brand,
            barcode=body.barcode,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="product not found")
        background_tasks.add_task(_sync_falkor_product, product_id, store.db_path, root)
        return updated

    @app.delete("/api/products/{product_id}", status_code=204)
    def delete_product(product_id: str, background_tasks: BackgroundTasks) -> None:
        _require_uuid(product_id)
        if not _RUN_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A crawl is running. Try deleting again when it finishes.")
        try:
            if store.get(product_id) is None:
                raise HTTPException(status_code=404, detail="product not found")
            output_root = root.resolve()
            folder = root / product_id
            resolved_folder = folder.resolve()
            if resolved_folder.parent != output_root or resolved_folder.name != product_id:
                raise HTTPException(status_code=400, detail="product folder is outside the output directory")
            if folder.is_dir():
                shutil.rmtree(folder)
            elif folder.exists():
                folder.unlink()
            try:
                remove_folder(urls_file, product_id)
            except UrlFileError as exc:
                raise HTTPException(status_code=500, detail=f"product files were removed, but the URL list could not be updated: {exc}") from exc
            if not store.delete(product_id):
                raise HTTPException(status_code=404, detail="product not found")
            background_tasks.add_task(_delete_falkor_product, product_id)
        finally:
            _RUN_LOCK.release()

    @app.get("/{product_id}/image")
    def product_image(product_id: str) -> FileResponse:
        _require_uuid(product_id)
        row = store.get(product_id)
        if row is None or not row.get("image_file"):
            raise HTTPException(status_code=404, detail="image not found")
        image = _safe_image(root / product_id, row["image_file"])
        if image is None:
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(image)

    @app.get("/{product_id}", response_class=HTMLResponse)
    def product_page(product_id: str) -> str:
        _require_uuid(product_id)
        row = store.get(product_id)
        if row is None:
            raise HTTPException(status_code=404, detail="product not found")
        return _page(row["name"] or "Product", _product_html(row))

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=PORT)


def _infer_search_method(payload: dict[str, Any]) -> str:
    brightdata = isinstance(payload.get("brightdata_crawler"), dict)
    if payload.get("origin") == "openai_prompt":
        return "openai_brightdata" if brightdata else "openai_api"
    return "base_crawler_brightdata" if brightdata else "base_crawler"


def _file_generated_at(path: Path) -> str:
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        timestamp = time.time()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds")


def _stamp_product_result(folder: Path, username: str, search_method: str) -> dict[str, Any]:
    path = folder / "product.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("product.json must contain an object")
    generated_at = record.get("search_generated_at") or _file_generated_at(path)
    record["owner_username"] = username
    record["search_generated_at"] = generated_at
    record["search_method"] = search_method
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def _backfill_result_metadata(username: str, db_path: Path, output_root: Path) -> None:
    """Assign legacy local results to the account that opened the authenticated UI."""
    store = ProductStore(db_path)
    changed_ids: list[str] = []
    for summary in store.list_products():
        product_id = summary["id"]
        row = store.get(product_id)
        if row is None:
            continue
        record = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        changed = False
        if not record.get("owner_username"):
            record["owner_username"] = username
            changed = True
        path = output_root / product_id / "product.json"
        if not record.get("search_generated_at"):
            record["search_generated_at"] = _file_generated_at(path) if path.is_file() else str(row.get("updated_at") or datetime.now(timezone.utc).isoformat())
            changed = True
        if not record.get("search_method"):
            record["search_method"] = _infer_search_method(record)
            changed = True
        if not changed:
            continue
        try:
            if path.is_file():
                path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                store.import_folder(path.parent)
            else:
                store.update_payload(product_id, record)
            changed_ids.append(product_id)
        except (OSError, ValueError):
            continue
    if changed_ids:
        _sync_falkor_products(changed_ids, db_path, output_root)


def _sync_falkor_product(product_id: str, db_path: Path, output_root: Path) -> None:
    _sync_falkor_products([product_id], db_path, output_root)


def _sync_falkor_products(product_ids: list[str], db_path: Path, output_root: Path) -> None:
    from product_crawler.falkor_sync import sync_product_ids

    sync_product_ids(product_ids, db_path, output_root)


def _delete_falkor_product(product_id: str) -> None:
    from product_crawler.falkor_sync import delete_product_nodes

    delete_product_nodes(product_id)


def _index_html(products: list[dict[str, Any]], *, search_error: str | None) -> str:
    crawled = [item for item in products if item.get("origin") != "openai_prompt"]
    prompted = [item for item in products if item.get("origin") == "openai_prompt"]
    url_items = "\n".join(_result_item(item, arrow=False) for item in crawled)
    prompt_items = "\n".join(_result_item(item, arrow=True) for item in prompted)
    items = "\n".join(part for part in (url_items, prompt_items) if part) or "<li>No products stored yet.</li>"
    error = ""
    if search_error:
        error = '<p class="search-error">OpenAI search did not return a product. Try a more specific description.</p>'
    return (
        '<p class="page-actions">'
        '<button type="button" id="page-reset">Reset</button>'
        '<label class="brightdata-toggle"><input type="checkbox" id="page-brightdata" value="true"> BrightData enrichment</label>'
        '<button type="button" id="page-run">Run</button>'
        '<form method="post" action="/logout"><button type="submit">Log out</button></form>'
        "</p>"
        '<form id="url-add" class="url-add">'
        '<label for="new-url">New URL</label>'
        '<input id="new-url" type="text" placeholder="https://example.com/product" autocomplete="off">'
        '<button type="submit">Add URL</button>'
        "</form>"
        '<p id="run-status" class="run-status"></p>'
        "<h1>Product Search Results : </h1>"
        '<p class="page-actions"><button type="button" id="delete-selected" disabled>Delete selected</button></p>'
        '<p class="index-note">Product search is performed by the built-in Crawler engine that scrapes the internet and then uses OpenAI APIs to complete the missing required data as well as additional searching on the product.</p>'
        '<p class="index-note index-note-arrow">An OpenAI tag means that page was generated from the text entered in the prompt. The built-in Crawler was not used.</p>'
        f'<ul id="product-results">{items}</ul>'
        '<section class="openai-section">'
        "<h1>Product Search by OpenAI API (Only) : </h1>"
        f"{error}"
        '<form class="index-form" method="post" action="/search">'
        '<label for="prompt">Describe the product</label>'
        '<textarea id="prompt" name="prompt" required maxlength="2000" placeholder="Describe the product you are searching for"></textarea>'
        '<label class="brightdata-toggle"><input type="checkbox" name="brightdata" value="true"> BrightData</label>'
        '<button type="submit">Search</button>'
        "</form>"
        "</section>"
        '<section id="jev-section" class="openai-section">'
        "<h1>Decision Engine by Jev API (Only) : </h1>"
        '<p class="index-note">Check the products that are the options. Decide asks Jev for a Choice, a Noul, and a Score on the question below, using each product page and the information stored for it.</p>'
        '<ul id="jev-selected"><li class="jev-empty">No products selected.</li></ul>'
        '<form id="jev-form" class="index-form">'
        '<label for="jev-question">Question</label>'
        '<textarea id="jev-question" maxlength="2000" placeholder="Question for the Choice, Noul, and Score"></textarea>'
        '<label class="brightdata-toggle"><input id="jev-falkordb" type="checkbox"> FalkorDB price and review analysis (optional; for pricing questions)</label>'
        '<button type="submit">Decide</button>'
        "</form>"
        '<div id="jev-decision" class="jev-decision"></div>'
        "</section>"
        "<script>"
        "const selected = new Map();"
        "const storageKey = 'jev-selected-ids';"
        "function storedIds() {"
        "  try { return JSON.parse(sessionStorage.getItem(storageKey) || '[]'); } catch (error) { return []; }"
        "}"
        "function remember() {"
        "  sessionStorage.setItem(storageKey, JSON.stringify([...selected.keys()]));"
        "}"
        "function render() {"
        "  const list = document.getElementById('jev-selected');"
        "  window.jevProducts = [...selected.values()];"
        "  list.replaceChildren();"
        "  if (selected.size === 0) {"
        "    const empty = document.createElement('li');"
        "    empty.className = 'jev-empty';"
        "    empty.textContent = 'No products selected.';"
        "    list.append(empty);"
        "    return;"
        "  }"
        "  for (const product of selected.values()) {"
        "    const item = document.createElement('li');"
        "    item.className = 'jev-card';"
        "    const link = document.createElement('a');"
        "    link.href = '/' + product.id;"
        "    link.textContent = product.name || product.id;"
        "    const detail = document.createElement('p');"
        "    detail.className = 'jev-detail';"
        "    const barcode = product.barcode ? ((product.barcode_type || 'Barcode') + ' ' + product.barcode) : 'No barcode';"
        "    const source = product.source_url || 'No source URL';"
        "    detail.textContent = [product.brand || 'No brand', barcode, source].join(' · ');"
        "    item.append(link, detail);"
        "    list.append(item);"
        "  }"
        "}"
        "async function useProduct(id, checked) {"
        "  if (!checked) { selected.delete(id); remember(); render(); return; }"
        "  const response = await fetch('/api/products/' + id);"
        "  if (!response.ok) return;"
        "  selected.set(id, await response.json());"
        "  remember();"
        "  render();"
        "}"
        "function updateDeleteButton() {"
        "  document.getElementById('delete-selected').disabled = !document.querySelector('#product-results .jev-select:checked');"
        "}"
        "document.querySelectorAll('.jev-select').forEach((box) => {"
        "  box.addEventListener('change', () => { useProduct(box.value, box.checked); updateDeleteButton(); });"
        "  if (storedIds().includes(box.value)) { box.checked = true; useProduct(box.value, true); }"
        "});"
        "updateDeleteButton();"
        "document.getElementById('page-reset').addEventListener('click', () => {"
        "  document.querySelectorAll('.jev-select').forEach((box) => { box.checked = false; });"
        "  updateDeleteButton();"
        "  selected.clear();"
        "  remember();"
        "  render();"
        "  document.getElementById('prompt').value = '';"
        "  document.getElementById('new-url').value = '';"
        "  document.getElementById('jev-question').value = '';"
        "  document.getElementById('jev-falkordb').checked = false;"
        "  document.getElementById('jev-decision').replaceChildren();"
        "  const searchError = document.querySelector('.search-error');"
        "  if (searchError) searchError.remove();"
        "  const status = document.getElementById('run-status');"
        "  status.textContent = '';"
        "  status.className = 'run-status';"
        "});"
        "document.getElementById('url-add').addEventListener('submit', async (event) => {"
        "  event.preventDefault();"
        "  const input = document.getElementById('new-url');"
        "  const status = document.getElementById('run-status');"
        "  const url = input.value.trim();"
        "  if (!url) {"
        "    status.className = 'run-status run-error';"
        "    status.textContent = 'Enter a URL.';"
        "    return;"
        "  }"
        "  const brightdata = document.getElementById('page-brightdata').checked;"
        "  const response = await fetch('/urls/add', {"
        "    method: 'POST',"
        "    headers: { 'Content-Type': 'application/json' },"
        "    body: JSON.stringify({ url, brightdata }),"
        "  });"
        "  let payload = {};"
        "  try { payload = await response.json(); } catch (error) { payload = {}; }"
        "  if (!response.ok) {"
        "    status.className = 'run-status run-error';"
        "    status.textContent = payload.detail || 'The URL was not saved.';"
        "    return;"
        "  }"
        "  input.value = '';"
        "  status.className = 'run-status';"
        "  if (payload.status === 'processed') {"
        "    status.textContent = 'That URL is already in urls.txt with a result.';"
        "    return;"
        "  }"
        "  if (payload.status === 'reprocess') {"
        "    status.textContent = 'BrightData reprocessing enabled. Hit Run to process it.';"
        "    return;"
        "  }"
        "  status.textContent = 'Saved to urls.txt. Hit Run to process it.';"
        "});"
        "function addResult(product) {"
        "  const list = document.getElementById('product-results');"
        "  const first = list.querySelector('li');"
        "  if (first && !first.querySelector('a')) first.remove();"
        "  const item = document.createElement('li');"
        "  const box = document.createElement('input');"
        "  box.className = 'jev-select';"
        "  box.type = 'checkbox';"
        "  box.value = product.id;"
        "  box.setAttribute('aria-label', 'Use ' + (product.name || product.id) + ' in Jev');"
        "  box.addEventListener('change', () => { useProduct(box.value, box.checked); updateDeleteButton(); });"
        "  const link = document.createElement('a');"
        "  link.href = '/' + product.id;"
        "  link.textContent = product.name || product.id;"
        "  item.append(box, document.createTextNode(' '), link);"
        "  const tag = document.createElement('span');"
        "  if (product.brightdata_enriched) {"
        "    tag.className = 'brightdata-tag';"
        "    tag.textContent = 'BrightData';"
        "    item.append(document.createTextNode(' '), tag);"
        "  } else if (!product.openai_result) {"
        "    tag.className = 'base-crawler-tag';"
        "    tag.textContent = 'Base Crawler';"
        "    item.append(document.createTextNode(' '), tag);"
        "  }"
        "  list.prepend(item);"
        "  updateDeleteButton();"
        "}"
        "document.getElementById('delete-selected').addEventListener('click', async () => {"
        "  const boxes = [...document.querySelectorAll('#product-results .jev-select:checked')];"
        "  if (!boxes.length) return;"
        "  const count = boxes.length;"
        "  if (!window.confirm('Delete ' + count + (count === 1 ? ' selected product and all its local files?' : ' selected products and all their local files?') + ' This cannot be undone.')) return;"
        "  const button = document.getElementById('delete-selected');"
        "  const status = document.getElementById('run-status');"
        "  button.disabled = true;"
        "  status.className = 'run-status';"
        "  status.textContent = 'Deleting selected products…';"
        "  let deleted = 0;"
        "  const errors = [];"
        "  for (const box of boxes) {"
        "    try {"
        "      const response = await fetch('/api/products/' + encodeURIComponent(box.value), { method: 'DELETE' });"
        "      if (!response.ok) {"
        "        let payload = {}; try { payload = await response.json(); } catch (error) {}"
        "        errors.push(payload.detail || ('Could not delete ' + box.value));"
        "        continue;"
        "      }"
        "      box.closest('li').remove();"
        "      selected.delete(box.value);"
        "      deleted += 1;"
        "    } catch (error) { errors.push('Could not delete ' + box.value); }"
        "  }"
        "  remember();"
        "  render();"
        "  const list = document.getElementById('product-results');"
        "  if (!list.querySelector('li')) { const empty = document.createElement('li'); empty.textContent = 'No products stored yet.'; list.append(empty); }"
        "  updateDeleteButton();"
        "  const lines = [];"
        "  if (deleted) lines.push('Deleted ' + deleted + (deleted === 1 ? ' product and its local files.' : ' products and their local files.'));"
        "  lines.push(...errors);"
        "  status.className = errors.length ? 'run-status run-error' : 'run-status';"
        "  status.textContent = lines.join(' ');"
        "});"
        "document.getElementById('page-run').addEventListener('click', () => { runPending(); });"
        "function setCrawlBusy(busy) {"
        "  document.getElementById('page-run').disabled = busy;"
        "  document.querySelector('#url-add button').disabled = busy;"
        "}"
        "async function runPending() {"
        "  const status = document.getElementById('run-status');"
        "  setCrawlBusy(true);"
        "  status.className = 'run-status';"
        "  status.textContent = 'Running…';"
        "  const brightdata = document.getElementById('page-brightdata').checked;"
        "  const response = await fetch('/urls/run?brightdata=' + (brightdata ? 'true' : 'false'), { method: 'POST' });"
        "  let payload = {};"
        "  try { payload = await response.json(); } catch (error) { payload = {}; }"
        "  setCrawlBusy(false);"
        "  if (!response.ok) {"
        "    status.className = 'run-status run-error';"
        "    status.textContent = payload.detail || 'The crawl did not finish.';"
        "    return;"
        "  }"
        "  const added = payload.added || [];"
        "  const errors = payload.errors || [];"
        "  for (const product of added) addResult(product);"
        "  if (!added.length && !errors.length) {"
        "    status.textContent = 'No unprocessed URLs in urls.txt.';"
        "    return;"
        "  }"
        "  const lines = [];"
        "  if (added.length) lines.push('Added ' + added.length + (added.length === 1 ? ' product.' : ' products.'));"
        "  for (const message of errors) lines.push(message);"
        "  status.className = errors.length ? 'run-status run-error' : 'run-status';"
        "  status.textContent = lines.join(' ');"
        "}"
        "function showDecision(message) {"
        "  const out = document.getElementById('jev-decision');"
        "  out.replaceChildren();"
        "  for (const line of String(message).split('\\n')) {"
        "    const row = document.createElement('p');"
        "    row.textContent = line;"
        "    out.append(row);"
        "  }"
        "}"
        "document.getElementById('jev-form').addEventListener('submit', async (event) => {"
        "  event.preventDefault();"
        "  const question = document.getElementById('jev-question').value.trim();"
        "  if (selected.size === 0) { showDecision('Check at least one product.'); return; }"
        "  if (!question) { showDecision('Enter a question.'); return; }"
        "  showDecision('Deciding…');"
        "  const response = await fetch('/jev/decide', {"
        "    method: 'POST',"
        "    headers: { 'Content-Type': 'application/json' },"
        "    body: JSON.stringify({ ids: [...selected.keys()], question, use_falkordb: document.getElementById('jev-falkordb').checked }),"
        "  });"
        "  let payload = {};"
        "  try { payload = await response.json(); } catch (error) { payload = {}; }"
        "  if (!response.ok) {"
        "    showDecision(payload.detail || 'Jev did not return a decision.');"
        "    return;"
        "  }"
        "  showChoice(payload);"
        "});"
        "function choicePercent(value) {"
        "  return Math.round(Number(value) * 100) + '%';"
        "}"
        "function decisionLine(text) {"
        "  const row = document.createElement('p');"
        "  row.textContent = text;"
        "  return row;"
        "}"
        "function decisionHeading(text) {"
        "  const row = document.createElement('h2');"
        "  row.textContent = text;"
        "  return row;"
        "}"
        "function showChoice(payload) {"
        "  const out = document.getElementById('jev-decision');"
        "  out.replaceChildren();"
        "  const score = payload.score || {};"
        "  const graphAnalysis = payload.falkordb_analysis;"
        "  const list = document.createElement('ul');"
        "  for (const option of payload.probabilities || []) {"
        "    const item = document.createElement('li');"
        "    item.textContent = option.name + ' — ' + choicePercent(option.probability);"
        "    list.append(item);"
        "  }"
        "  const levels = document.createElement('ul');"
        "  for (const level of score.levels || []) {"
        "    const item = document.createElement('li');"
        "    item.textContent = level.description + ' — ' + choicePercent(level.probability);"
        "    levels.append(item);"
        "  }"
        "  const top = (score.levels || []).length ? (score.levels.length - 1) : '';"
        "  const scoreText = score.score == null ? '' : Number(score.score).toFixed(2) + (top === '' ? '' : ' of ' + top);"
        "  const no = payload.noul == null ? '' : choicePercent(1 - Number(payload.noul));"
        "  out.append("
        "    decisionHeading('Choice'),"
        "    decisionLine('Selected: ' + (payload.product_name || payload.choice || '')),"
        "    decisionLine('Confidence: ' + (payload.confidence == null ? '' : choicePercent(payload.confidence))),"
        "    decisionLine('Probability for each option'),"
        "    list,"
        "    decisionHeading('Noul'),"
        "    decisionLine('Yes: ' + (payload.noul == null ? '' : choicePercent(payload.noul))),"
        "    decisionLine('No: ' + no),"
        "    decisionHeading('Score'),"
        "    decisionLine('Score: ' + scoreText),"
        "    decisionLine('Confidence: ' + (score.confidence == null ? '' : choicePercent(score.confidence))),"
        "    decisionLine('Probability for each level'),"
        "    levels"
        "  );"
        "  if (graphAnalysis) {"
        "    const section = document.createElement('section');"
        "    section.className = 'jev-pricing-analysis';"
        "    section.append(decisionHeading('Pricing Decision Analysis'));"
        "    const picked = (payload.probabilities || []).find((item) => item.id === payload.choice);"
        "    const pickedProbability = picked ? choicePercent(picked.probability) : 'not returned';"
        "    section.append(decisionLine('Jev selected ' + (payload.product_name || payload.choice || 'no product') + ' with ' + pickedProbability + ' choice probability and ' + (payload.confidence == null ? 'unreported' : choicePercent(payload.confidence)) + ' choice confidence.'));"
        "    section.append(decisionHeading('LLM explanation'));"
        "    for (const line of insightLines(payload.insight)) section.append(line);"
        "    section.append(decisionHeading('FalkorDB evidence sent to Jev'));"
        "    const evidence = document.createElement('ul');"
        "    for (const product of graphAnalysis.products || []) {"
        "      const price = product.price_analysis || {};"
        "      const sentiment = product.review_sentiment || {};"
        "      const brand = product.brand_graph_context || {};"
        "      const priceText = product.price == null ? 'price unavailable' : ((product.currency || 'currency unknown') + ' ' + product.price);"
        "      const medianText = price.selected_set_median == null ? 'no same-currency comparison' : ('selected-set median ' + product.currency + ' ' + price.selected_set_median + '; ' + (price.difference_from_selected_median_percent > 0 ? '+' : '') + price.difference_from_selected_median_percent + '% vs median; rank ' + price.rank_low_to_high + ' of ' + price.compared_products_same_currency);"
        "      const tone = sentiment.score_minus1_to_1 == null ? 'review sentiment unavailable' : ('rating sentiment ' + Number(sentiment.score_minus1_to_1).toFixed(2) + ' from ' + sentiment.rated_review_count + ' rated reviews (+' + sentiment.positive_reviews + ' positive, ' + sentiment.neutral_reviews + ' neutral, ' + sentiment.negative_reviews + ' negative)');"
        "      const valueMentions = 'price/value comment keyword matches: ' + sentiment.price_value_positive_mentions + ' positive, ' + sentiment.price_value_negative_mentions + ' negative';"
        "      const brandText = 'brand graph: ' + brand.products_in_graph_for_brand + ' products; PageRank ' + (brand.pagerank_centrality == null ? 'unavailable' : brand.pagerank_centrality);"
        "      const item = document.createElement('li');"
        "      item.textContent = [product.name, priceText, medianText, tone, valueMentions, brandText].join(' · ');"
        "      evidence.append(item);"
        "    }"
        "    section.append(evidence);"
        "    section.append(decisionLine('Sentiment score ranges from -1 (lower-rated) to +1 (higher-rated) and is derived from star ratings. Price/value mentions use keyword matching. PageRank describes graph connections, not product quality. Jev does not expose per-signal weights, so these inputs explain the evidence it received but cannot prove what caused its choice.'));"
        "    out.append(section);"
        "  } else {"
        "    out.append(decisionHeading('LLM (with Jev input) :'), ...insightLines(payload.insight));"
        "  }"
        "}"
        "function insightLines(text) {"
        "  const lines = String(text || '').split(/\\n+/).map((line) => line.trim()).filter(Boolean);"
        "  if (!lines.length) lines.push('OpenAI did not return an explanation.');"
        "  return lines.map((line) => {"
        "    const row = decisionLine(line);"
        "    row.className = 'jev-insight';"
        "    return row;"
        "  });"
        "}"
        "</script>"
    )


def _result_item(item: dict[str, Any], *, arrow: bool) -> str:
    product_id = html.escape(item["id"])
    name = html.escape(item["name"] or item["id"])
    if arrow:
        marker = ' <span class="openai-tag">OpenAI</span>'
    elif item.get("brightdata_enriched"):
        marker = ' <span class="brightdata-tag">BrightData</span>'
    else:
        marker = ' <span class="base-crawler-tag">Base Crawler</span>'
    klass = ' class="openai-result"' if arrow else ""
    return (
        f"<li{klass}>"
        f'<input class="jev-select" type="checkbox" value="{product_id}" aria-label="Use {name} in Jev"> '
        f'<a href="/{product_id}">{name}</a>'
        f"{marker}"
        "</li>"
    )


def _default_accounts_path() -> Path:
    candidates = (Path("configs/user_accounts.rtf"), Path(__file__).resolve().parents[2] / "configs" / "user_accounts.rtf")
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _load_accounts(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    text = re.sub(r"\\'[0-9a-fA-F]{2}", lambda match: bytes([int(match.group(0)[2:], 16)]).decode("cp1252"), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text).replace("{", "").replace("}", "")
    accounts: dict[str, str] = {}
    pattern = re.compile(
        r"username\s*:\s*([^,\\\r\n]+),\s*userpassword\s*:\s*([^\\\r\n]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        username = match.group(1).strip().casefold()
        password = match.group(2).strip()
        if username and password:
            accounts[username] = password
    return accounts


def _make_session(username: str) -> str:
    expires = int(time.time()) + _SESSION_MAX_AGE
    payload = f"{username}\n{expires}".encode("utf-8")
    signature = hmac.new(_SESSION_SECRET, payload, hashlib.sha256).hexdigest().encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    return encoded.decode("ascii") + "." + signature.decode("ascii")


def _session_username(token: str | None, accounts_path: Path) -> str | None:
    if not token:
        return None
    try:
        encoded, supplied_signature = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        username, expiry = payload.decode("utf-8").split("\n", 1)
        expected = hmac.new(_SESSION_SECRET, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_signature) or int(expiry) < int(time.time()):
            return None
        if username not in _load_accounts(accounts_path):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return username


def _safe_next(value: str) -> str:
    target = value.strip()
    if not target.startswith("/") or target.startswith(("//", "/\\")) or "\r" in target or "\n" in target:
        return "/"
    return target


def _login_html(*, next: str, error: str | None = None) -> str:
    error_html = f'<p class="login-error">{html.escape(error)}</p>' if error else ""
    body = (
        '<section class="login-card">'
        "<h1>Product Search Login</h1>"
        f"{error_html}"
        '<form method="post" action="/login" autocomplete="on">'
        f'<input type="hidden" name="next" value="{html.escape(next, quote=True)}">'
        '<label for="username">Username</label>'
        '<input id="username" name="username" type="email" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus>'
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        '<button type="submit">Log in</button>'
        "</form>"
        "</section>"
    )
    return _page("Product Search Login", body)


def _default_urls_path() -> Path:
    candidates = (Path("urls.txt"), Path(__file__).resolve().parents[2] / "urls.txt")
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _require_uuid(product_id: str) -> None:
    if not UUID_RE.match(product_id):
        raise HTTPException(status_code=404, detail="product not found")


def _safe_image(folder: Path, relative: str) -> Path | None:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        return None
    folder = folder.resolve()
    image = (folder / relative).resolve()
    if folder not in image.parents or not image.is_file():
        return None
    return image


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 40rem; color: #1c1c1c; }}
    .page-actions {{ margin: 0 0 0.6rem; display: flex; gap: 0.6rem; }}
    .page-actions button, .url-add button {{ font: inherit; padding: 0.35rem 0.8rem; }}
    .url-add {{ display: flex; gap: 0.6rem; align-items: center; margin: 0 0 0.6rem; }}
    .url-add label {{ white-space: nowrap; }}
    .url-add input {{ flex: 1; font: inherit; font-size: 0.95rem; padding: 0.35rem 0.5rem; }}
    .run-status {{ margin: 0 0 1.25rem; min-height: 1.2rem; }}
    .run-error {{ color: #8a1f1f; }}
    .login-card {{ max-width: 24rem; margin: 4rem auto; padding: 1.5rem; border: 1px solid #ddd; border-radius: 8px; }}
    .login-card form {{ display: grid; gap: 0.55rem; }}
    .login-card input {{ font: inherit; padding: 0.45rem 0.55rem; margin-bottom: 0.55rem; }}
    .login-card button {{ font: inherit; padding: 0.45rem 0.8rem; margin-top: 0.4rem; }}
    .login-error {{ color: #8a1f1f; }}
    a {{ color: #0b57d0; }}
    .index-note {{ margin: 0.35rem 0 0.35rem; font-size: 0.85rem; line-height: 1.4; color: #555; }}
    .section-subtitle {{ margin: -0.35rem 0 1rem; font-size: 0.82rem; color: #555; }}
    .index-note-arrow {{ margin-bottom: 1.25rem; }}
    .openai-section {{ margin-top: 2.5rem; padding-top: 1.25rem; border-top: 1px solid #ddd; }}
    .index-form textarea {{ width: 100%; min-height: 6rem; box-sizing: border-box; font: inherit; font-size: 0.95rem; padding: 0.5rem; }}
    .index-form button {{ margin-top: 0.6rem; font: inherit; padding: 0.35rem 0.8rem; }}
    .search-error {{ color: #8a1f1f; font-size: 0.9rem; }}
    .openai-result {{ margin: 0.35rem 0; }}
    .openai-tag {{ display: inline-block; border: 1px solid #6d42a2; color: #fff; background: #8056b3; padding: 0.15rem 0.45rem; border-radius: 4px; font-size: 0.78rem; font-weight: 700; margin: 0 0.25rem; vertical-align: middle; }}
    .brightdata-tag {{ display: inline-block; border: 1px solid #245fa8; color: #fff; background: #3f7fc4; padding: 0.15rem 0.45rem; border-radius: 4px; font-size: 0.78rem; font-weight: 700; margin: 0 0.25rem; vertical-align: middle; }}
    .base-crawler-tag {{ display: inline-block; border: 1px solid #55704b; color: #fff; background: #718f65; padding: 0.15rem 0.45rem; border-radius: 4px; font-size: 0.78rem; font-weight: 700; margin: 0 0.25rem; vertical-align: middle; }}
    .brightdata-section {{ margin-top: 2rem; padding-top: 0.25rem; border-top: 1px solid #ddd; }}
    .brightdata-section h2 {{ font-size: 1rem; margin-bottom: 0.45rem; }}
    .brightdata-section p {{ margin: 0.25rem 0; }}
    .brightdata-sources {{ margin: 0.4rem 0 0; padding-left: 1.25rem; }}
    .brightdata-subsection {{ margin-top: 1rem; padding: 0.75rem; border: 1px solid #ddd; border-radius: 6px; }}
    .brightdata-subsection h3 {{ font-size: 0.95rem; margin: 0 0 0.35rem; }}
    .brightdata-snippet {{ color: #555; }}
    .brightdata-note {{ color: #555; font-size: 0.85rem; }}
    .brightdata-extra-label {{ list-style: none; margin-left: -1.25rem; }}
    .trend-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: 0.75rem; }}
    .trend-metric {{ min-width: 0; }}
    .trend-metric h4 {{ margin: 0.5rem 0 0.25rem; font-size: 0.88rem; }}
    .trend-chart {{ width: 100%; height: 110px; background: #fafafa; border: 1px solid #e4e4e4; border-radius: 6px; }}
    .trend-axis {{ stroke: #ddd; stroke-width: 1; }}
    .trend-range {{ display: flex; justify-content: space-between; color: #666; font-size: 0.72rem; margin-top: 0.2rem; }}
    .brightdata-sources li {{ margin: 0.25rem 0; word-break: break-all; }}
    .brightdata-empty {{ color: #555; }}
    .jev-select {{ margin-right: 0.45rem; }}
    .jev-empty {{ color: #555; }}
    .jev-card {{ margin: 0.55rem 0 0.9rem; }}
    .jev-detail {{ margin: 0.15rem 0 0; color: #555; font-size: 0.85rem; word-break: break-all; }}
    .jev-decision {{ margin-top: 1rem; font-size: 1.05rem; }}
    .jev-decision h2 {{ font-size: 1.05rem; margin: 1rem 0 0.35rem; }}
    .jev-decision h2:first-child {{ margin-top: 0; }}
    .jev-decision p {{ margin: 0.2rem 0; }}
    .jev-insight {{ margin: 0.55rem 0; line-height: 1.45; }}
    .jev-pricing-analysis {{ margin-top: 1.25rem; padding: 1rem; border: 1px solid #b9cbe4; border-radius: 8px; background: #f6f9fd; }}
    .jev-pricing-analysis li {{ margin: 0.6rem 0; line-height: 1.45; }}
    img.product {{ width: 160px; height: 160px; object-fit: contain; background: #f3f3f3; }}
    .field-label {{ margin: 1rem 0 0; color: #555; }}
    .brand {{ font-size: 1.3rem; margin: 0.15rem 0 0; letter-spacing: 0.04em; }}
    .barcode-type {{ margin: 1rem 0 0; color: #555; }}
    .barcode {{ font-family: ui-monospace, monospace; font-size: 1.3rem; margin: 0.15rem 0 0.25rem; }}
    .barcode-note {{ margin: 0 0 1.5rem; color: #555; }}
    .barcode-openai {{ margin: 0.35rem 0 1.5rem; padding: 0.55rem 0.7rem; background: #fff3bf; border: 1px solid #e6c200; color: #5c4300; }}
    .source-url {{ word-break: break-all; margin: 0.15rem 0 0; }}
    .blocked-note {{ margin: 1rem 0 0; padding: 0.75rem; background: #fff6e8; border: 1px solid #e6d3b0; border-radius: 6px; }}
    .feedback {{ margin-top: 2rem; padding-top: 0.25rem; border-top: 1px solid #ddd; }}
    .feedback-list {{ max-height: 18rem; overflow-y: auto; margin-top: 0.5rem; padding: 0 0.85rem; border: 1px solid #ddd; border-radius: 6px; background: #fafafa; }}
    .feedback-item {{ padding: 0.85rem 0; border-bottom: 1px solid #e4e4e4; }}
    .feedback-item:last-child {{ border-bottom: 0; }}
    .feedback-meta {{ margin: 0; color: #555; font-size: 0.9rem; }}
    .feedback-text {{ margin: 0.35rem 0 0; white-space: pre-wrap; }}
    .videos {{ margin-top: 2rem; padding-top: 0.25rem; border-top: 1px solid #ddd; }}
    .video-list {{ margin: 0.5rem 0 0; padding: 0; list-style: none; }}
    .video-item {{ padding: 0.75rem 0; border-bottom: 1px solid #e4e4e4; }}
    .video-item:last-child {{ border-bottom: 0; }}
    .video-link {{ word-break: break-all; }}
    .research {{ margin-top: 2rem; padding-top: 0.25rem; border-top: 1px solid #ddd; }}
    .research-subtitle {{ margin: -0.35rem 0 0.7rem; font-size: 0.82rem; color: #555; }}
    .research-list {{ margin-top: 0.5rem; padding: 0 0.85rem; border: 1px solid #ddd; border-radius: 6px; background: #fafafa; }}
    .research-sources {{ margin: 0.35rem 0 0; padding-left: 1.1rem; }}
    .research-sources li {{ margin: 0.2rem 0; word-break: break-all; }}
    .research-item {{ padding: 0.75rem 0; border-bottom: 1px solid #e4e4e4; }}
    .research-item:last-child {{ border-bottom: 0; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _product_html(row: dict[str, Any]) -> str:
    product_id = row["id"]
    name = row["name"] or "Untitled product"
    image = ""
    if row.get("image_file"):
        image = (
            f'<img class="product" alt="{html.escape(name)}" '
            f'src="/{html.escape(product_id)}/image">'
        )
    if row.get("brand"):
        brand_html = (
            '<p class="field-label">Brand</p>'
            f'<p class="brand" id="brand-value">{html.escape(row["brand"])}</p>'
        )
    else:
        brand_html = '<p class="brand" id="brand-value">No brand on this page</p>'
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if row.get("barcode"):
        kind = row["barcode_type"] or "barcode"
        note = payload.get("barcode_note")
        notes = []
        if payload.get("barcode_from_openai"):
            notes.append(
                '<p class="barcode-openai" id="barcode-openai">Not found on this page. OpenAI found it.</p>'
            )
        if isinstance(note, str) and note.strip() and note.strip() != "Not found on this page. OpenAI found it.":
            notes.append(f'<p class="barcode-note">{html.escape(note.strip())}</p>')
        barcode_html = (
            f'<p class="barcode-type" id="barcode-type">{html.escape(kind)}</p>'
            f'<p class="barcode" id="barcode-value">{html.escape(row["barcode"])}</p>'
            f"{''.join(notes)}"
        )
    else:
        barcode_html = '<p class="barcode" id="barcode-value">No barcode on this page</p>'
    source = row.get("source_url") or payload.get("requested_url")
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        source_html = (
            '<p class="field-label">Source</p>'
            f'<p class="source-url"><a id="source-url" href="{html.escape(source)}">'
            f"{html.escape(source)}</a></p>"
        )
    else:
        source_html = '<p class="source-url" id="source-url">No source URL stored</p>'
    note = payload.get("blocked_note")
    fetched = payload.get("fetched_url")
    if isinstance(note, str) and note.strip():
        fetched_html = ""
        if isinstance(fetched, str) and fetched.startswith(("http://", "https://")):
            fetched_html = (
                '<p class="source-url">Read from '
                f'<a id="fetched-url" href="{html.escape(fetched)}">{html.escape(fetched)}</a></p>'
            )
        source_html += (
            f'<p class="blocked-note" id="blocked-note">{html.escape(note.strip())}</p>'
            f"{fetched_html}"
        )
    brightdata_tag = '<span class="brightdata-tag">BrightData</span>' if isinstance(payload.get("brightdata_crawler"), dict) else ""
    return f"""
<p><a href="/">All products</a></p>
{image}
{brightdata_tag}
<h1 id="product-name">{html.escape(name)}</h1>
{brand_html}
{barcode_html}
{source_html}
{_feedback_section(payload)}
{_video_section(payload)}
{_brightdata_section(payload)}
{_research_section(payload)}
"""


def _brightdata_section(payload: dict[str, Any]) -> str:
    data = payload.get("brightdata_crawler")
    if not isinstance(data, dict):
        return ""
    query = data.get("query")
    status = data.get("status") or "not_found"
    identifiers = data.get("identifiers") if isinstance(data.get("identifiers"), list) else []
    details = [f'<p class="brightdata-status">Status: {html.escape(str(status))}</p>']
    if isinstance(query, str) and query.strip():
        details.append(f'<p class="brightdata-query"><strong>Search:</strong> {html.escape(query.strip())}</p>')
    if identifiers:
        details.append('<p class="brightdata-identifiers"><strong>Identifiers:</strong> ' + html.escape(", ".join(str(value) for value in identifiers[:8])) + "</p>")
    details.append(_brightdata_items_subsection("1. Product Ranking Evidence", data.get("ranking_evidence"), "items", note_key="note"))
    details.append(_brightdata_items_subsection("2. User Feedback", data.get("feedback"), "items"))

    manuals = data.get("manuals_and_instructions") if isinstance(data.get("manuals_and_instructions"), dict) else {}
    manual_items = manuals.get("manuals") if isinstance(manuals.get("manuals"), list) else []
    instruction_items = manuals.get("instructions") if isinstance(manuals.get("instructions"), list) else []
    details.append(_brightdata_items_subsection("3. Manuals and How-To Instructions", {"status": manuals.get("status", "not_found"), "manuals": manual_items, "instructions": instruction_items}, "manuals", extra_items=instruction_items, extra_label="How-To Instructions"))
    details.append(_brightdata_items_subsection("4. Pricing and Availability", data.get("pricing"), "items"))
    details.append(_brightdata_items_subsection("5. Product Identity and Specifications", data.get("identity_and_specifications"), "items"))
    details.append(_brightdata_items_subsection("6. Retailer and Ecommerce Sources", data.get("retailer_sources"), "items"))
    details.append(_brightdata_trend_section(data.get("trend_analysis")))
    return '<section id="brightdata-crawler" class="brightdata-section"><h2>BrightData Crawler</h2>' + "".join(details) + "</section>"


def _brightdata_trend_section(data: Any) -> str:
    trend = data if isinstance(data, dict) else {}
    observations = trend.get("observations") if isinstance(trend.get("observations"), list) else []
    assessment = trend.get("assessment") if isinstance(trend.get("assessment"), dict) else {}
    summary = str(assessment.get("summary") or "Insufficient observations to assess market direction.")
    direction = str(assessment.get("direction") or "unknown")
    confidence = str(assessment.get("confidence") or "low")

    metrics = (
        ("Price", "price", "#8f3218"),
        ("Rating", "rating", "#36527f"),
        ("Review count", "review_count", "#4c7a4b"),
    )
    graphs: list[str] = []
    for label, key, color in metrics:
        points = [(index, float(obs[key])) for index, obs in enumerate(observations) if isinstance(obs.get(key), (int, float))]
        if len(points) < 2:
            graphs.append(f'<div class="trend-metric"><h4>{html.escape(label)}</h4><p class="brightdata-empty">Insufficient historical observations.</p></div>')
            continue
        values = [value for _, value in points]
        minimum, maximum = min(values), max(values)
        spread = maximum - minimum if maximum != minimum else 1.0
        max_index = max(len(points) - 1, 1)
        coordinates = " ".join(f"{(index / max_index) * 600:.0f},{90 - ((value - minimum) / spread) * 70:.1f}" for index, (_, value) in enumerate(points))
        labels = ""
        if observations and isinstance(observations[-1].get("timestamp"), str):
            labels = '<div class="trend-range"><span>' + html.escape(observations[0].get("timestamp", "")[:10]) + '</span><span>' + html.escape(observations[-1].get("timestamp", "")[:10]) + '</span></div>'
        graphs.append(
            f'<div class="trend-metric"><h4>{html.escape(label)}</h4>'
            '<svg class="trend-chart" viewBox="0 0 600 110" role="img" aria-label="' + html.escape(label + " trend") + '">'
            '<line x1="0" y1="90" x2="600" y2="90" class="trend-axis"></line>'
            '<polyline points="' + coordinates + '" fill="none" stroke="' + color + '" stroke-width="3"></polyline>'
            '</svg>' + labels + '</div>'
        )
    return (
        '<article class="brightdata-subsection trend-subsection"><h3>7. Product Market Trend Graph</h3>'
        '<p><strong>Direction:</strong> ' + html.escape(direction.title()) + ' · <strong>Confidence:</strong> ' + html.escape(confidence.title()) + '</p>'
        '<p class="brightdata-note">' + html.escape(summary) + '</p>'
        '<div class="trend-grid">' + "".join(graphs) + '</div>'
        '<p class="brightdata-empty">Historical data grows only when Bright Data scans this product again. No synthetic trend points are generated.</p>'
        '</article>'
    )


def _brightdata_items_subsection(title: str, data: Any, item_key: str, *, extra_items: list[Any] | None = None, extra_label: str = "Additional Sources", note_key: str | None = None) -> str:
    section = data if isinstance(data, dict) else {}
    status = html.escape(str(section.get("status", "not_found")))
    items = section.get(item_key) if isinstance(section.get(item_key), list) else []
    note = ""
    if note_key and isinstance(section.get(note_key), str):
        note = '<p class="brightdata-note">' + html.escape(section[note_key]) + "</p>"
    source_items: list[str] = []
    for item in items:
        source_items.extend(_brightdata_source_item(item))
    if extra_items:
        source_items.append(f'<li class="brightdata-extra-label"><strong>{html.escape(extra_label)}</strong></li>')
        for item in extra_items:
            source_items.extend(_brightdata_source_item(item))
    if not source_items:
        content = '<p class="brightdata-empty">No verified {0} results returned.</p>'.format(html.escape(title.lower().replace("1. ", "").replace("2. ", "").replace("3. ", "").replace("4. ", "").replace("5. ", "").replace("6. ", "")))
    else:
        content = '<ul class="brightdata-sources">' + "".join(source_items) + "</ul>"
    return '<article class="brightdata-subsection"><h3>' + html.escape(title) + '</h3><p class="brightdata-status">Status: ' + status + "</p>" + note + content + "</article>"


def _brightdata_source_item(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return []
    title = str(item.get("title") or url)
    snippet = str(item.get("snippet") or "")
    text = '<li><a href="' + html.escape(url, quote=True) + '" target="_blank" rel="noopener">' + html.escape(title) + "</a>"
    if snippet:
        text += '<span class="brightdata-snippet"> — ' + html.escape(snippet[:500]) + "</span>"
    return [text + "</li>"]


def _feedback_section(payload: dict[str, Any]) -> str:
    reviews = payload.get("reviews") if isinstance(payload.get("reviews"), list) else []
    items: list[str] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        comments = review.get("comments")
        if not isinstance(comments, str) or not comments.strip():
            continue
        meta = " · ".join(
            part
            for part in (
                review.get("nickname") if isinstance(review.get("nickname"), str) else None,
                _review_date(review.get("created_at")),
                _review_rating(review.get("rating")),
            )
            if part
        )
        headline = review.get("headline") if isinstance(review.get("headline"), str) else ""
        headline_html = f"<p><strong>{html.escape(headline.strip())}</strong></p>" if headline.strip() else ""
        meta_html = f'<p class="feedback-meta">{html.escape(meta)}</p>' if meta else ""
        items.append(
            '<article class="feedback-item">'
            f"{meta_html}{headline_html}"
            f'<p class="feedback-text">{html.escape(comments.strip())}</p>'
            "</article>"
        )
    if items:
        body = "".join(items)
    else:
        note = payload.get("reviews_note")
        message = note.strip() if isinstance(note, str) and note.strip() else "No user feedback found on this page."
        body = f'<p class="feedback-text">{html.escape(message)}</p>'
    return (
        '<section class="feedback" id="feedback" aria-label="User feedback">'
        "<h2>User feedback</h2>"
        f'<div class="feedback-list">{body}</div>'
        "</section>"
    )


def _video_section(payload: dict[str, Any]) -> str:
    videos = payload.get("videos")
    note = payload.get("videos_note")
    if not isinstance(videos, list) and not (isinstance(note, str) and note.strip()):
        return ""
    items: list[str] = []
    if isinstance(videos, list):
        for video in videos[:2]:
            if not isinstance(video, dict):
                continue
            url = video.get("url")
            title = video.get("title")
            label = title.strip() if isinstance(title, str) and title.strip() else "Video search result"
            description = video.get("description")
            description_html = (
                f'<p class="feedback-text">{html.escape(description.strip())}</p>'
                if isinstance(description, str) and description.strip()
                else ""
            )
            tags = video.get("tags")
            tags_html = (
                f'<p class="feedback-meta">Tags: {html.escape(", ".join(tag for tag in tags if isinstance(tag, str)))}</p>'
                if isinstance(tags, list) and any(isinstance(tag, str) and tag.strip() for tag in tags)
                else ""
            )
            category = video.get("category")
            category_html = (
                f'<p class="feedback-meta">{html.escape(category.strip())}</p>'
                if isinstance(category, str) and category.strip()
                else ""
            )
            link_html = (
                f'<a class="video-link" href="{html.escape(url)}">Search result</a>'
                if isinstance(url, str) and url.startswith(("http://", "https://"))
                else ""
            )
            items.append(
                '<li class="video-item">'
                f"{category_html}"
                f"<p>{html.escape(label)}</p>"
                f"{description_html}"
                f"{tags_html}"
                f"{link_html}</li>"
            )
    if items:
        body = f'<ul class="video-list">{"".join(items)}</ul>'
    else:
        message = note.strip() if isinstance(note, str) and note.strip() else "No related video descriptions or tags found."
        body = f'<p class="feedback-text">{html.escape(message)}</p>'
    return (
        '<section class="videos" id="videos" aria-label="Video descriptions and tags">'
        "<h2>Video descriptions and tags</h2>"
        f"{body}"
        "</section>"
    )


_RESEARCH_LABELS = (
    ("audience_gender", "Who it is for"),
    ("popularity", "Popularity"),
    ("age_group", "Age group"),
    ("professionals", "Professionals"),
    ("demographic_appeal", "Demographic appeal"),
    ("top_us_state", "US state where it sells most"),
    ("top_us_city", "City in that state"),
    ("health_benefits", "Health benefits"),
    ("average_price", "Average market price"),
    ("bundle", "Often bought with"),
    ("recalls", "Recalls and public complaints"),
)


def _research_section(payload: dict[str, Any]) -> str:
    research = payload.get("research")
    if not isinstance(research, dict):
        return ""
    items = []
    for key, label in _RESEARCH_LABELS:
        answer = research.get(key)
        text = answer.strip() if isinstance(answer, str) and answer.strip() else "Unknown"
        items.append(
            '<article class="research-item">'
            f'<p class="feedback-meta">{html.escape(label)}</p>'
            f'<p class="feedback-text">{html.escape(text)}</p>'
            "</article>"
        )
    sources = research.get("sources")
    if isinstance(sources, list):
        links = []
        for url in sources:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                links.append(f'<li><a href="{html.escape(url)}">{html.escape(url)}</a></li>')
        if links:
            items.append(
                '<article class="research-item">'
                '<p class="feedback-meta">Sources</p>'
                f'<ul class="research-sources">{"".join(links)}</ul>'
                "</article>"
            )
    return (
        '<section class="research" id="research" aria-label="Product research">'
        "<h2>Product Sentiment and Trend Analysis</h2>"
        '<p class="research-subtitle">(BrightData + OpenAI + Base Crawler)</p>'
        f'<div class="research-list">{"".join(items)}</div>'
        "</section>"
    )


def _review_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:10]


def _review_rating(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return f"{value:g} stars"
    if isinstance(value, str) and value.strip():
        return f"{value.strip()} stars"
    return None


if __name__ == "__main__":
    main()
