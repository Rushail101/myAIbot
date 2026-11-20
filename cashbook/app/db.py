"""Thin async wrapper over Supabase's REST API (PostgREST) and Storage API. No extra SDK needed."""
import logging
import os
import uuid
from typing import Any, Optional

import httpx

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TABLE = "cashbook_entries"
BILLS_BUCKET = os.environ.get("BILLS_BUCKET", "cashbook-bills")  # private storage bucket for bill photos/PDFs

log = logging.getLogger("cashbook")
_client: Optional[httpx.AsyncClient] = None
_storage: Optional[httpx.AsyncClient] = None


def _c() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=f"{URL}/rest/v1",
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            timeout=20,
        )
    return _client


def _s() -> httpx.AsyncClient:
    global _storage
    if _storage is None:
        _storage = httpx.AsyncClient(
            base_url=f"{URL}/storage/v1",
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
            timeout=60,
        )
    return _storage


async def insert(row: dict) -> dict:
    r = await _c().post(f"/{TABLE}", json=row, headers={"Prefer": "return=representation"})
    if r.status_code == 400 and "entry_time" in row:  # column not migrated yet -> save without the time
        row = {k: v for k, v in row.items() if k != "entry_time"}
        r = await _c().post(f"/{TABLE}", json=row, headers={"Prefer": "return=representation"})
    r.raise_for_status()
    return r.json()[0]


async def get(entry_id: int) -> Optional[dict]:
    r = await _c().get(f"/{TABLE}", params={"id": f"eq.{entry_id}", "limit": 1})
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


async def get_by_msg(tg_msg_id: int) -> Optional[dict]:
    r = await _c().get(f"/{TABLE}", params={"tg_msg_id": f"eq.{tg_msg_id}", "limit": 1})
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


async def get_by_ref(upi_ref: str) -> Optional[dict]:
    r = await _c().get(f"/{TABLE}", params={"upi_ref": f"eq.{upi_ref}", "limit": 1})
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


async def update(entry_id: int, fields: dict) -> dict:
    r = await _c().patch(
        f"/{TABLE}", params={"id": f"eq.{entry_id}"}, json=fields, headers={"Prefer": "return=representation"}
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else {}


async def delete(entry_id: int) -> None:
    e = await get(entry_id)
    r = await _c().delete(f"/{TABLE}", params={"id": f"eq.{entry_id}"})
    r.raise_for_status()
    if e and e.get("bill_paths"):
        await remove_files(e["bill_paths"])


# ---------- bills (Supabase Storage) ----------
async def add_bill(entry_id: int, data: bytes, ext: str, content_type: str) -> dict:
    """Uploads one bill page and links it to the entry. Returns the updated entry."""
    path = f"{entry_id}/{uuid.uuid4().hex}.{ext.lower().lstrip('.') or 'jpg'}"
    r = await _s().post(f"/object/{BILLS_BUCKET}/{path}", content=data, headers={"Content-Type": content_type})
    r.raise_for_status()
    try:
        e = await get(entry_id)
        if e is None:
            raise LookupError("entry no longer exists")
        return await update(entry_id, {"bill_paths": list(e.get("bill_paths") or []) + [path]})
    except Exception:
        await remove_files([path])  # don't leave an orphan file behind
        raise


async def bill_urls(paths: list[str], expires_in: int = 3600) -> list[str]:
    """Short-lived links to view private bill files."""
    if not paths:
        return []
    r = await _s().post(f"/object/sign/{BILLS_BUCKET}", json={"expiresIn": expires_in, "paths": paths})
    r.raise_for_status()
    urls = []
    for item in r.json():
        signed = item.get("signedURL") or item.get("signedUrl")
        if signed:
            urls.append(signed if signed.startswith("http") else f"{URL}/storage/v1{signed}")
    return urls


async def remove_files(paths: list[str]) -> None:
    try:
        r = await _s().request("DELETE", f"/object/{BILLS_BUCKET}", json={"prefixes": paths})
        r.raise_for_status()
    except Exception as exc:  # a leftover file is harmless; don't fail the caller
        log.warning("Couldn't remove bill files %s: %r", paths, exc)


async def list_entries(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    mode: Optional[str] = None,
    direction: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 1000,
    order: str = "entry_date.desc,id.desc",
    offset: int = 0,
    select: Optional[str] = None,
) -> list[dict]:
    params: list[tuple[str, Any]] = [("order", order), ("limit", limit)]
    if offset:
        params.append(("offset", offset))
    if select:
        params.append(("select", select))
    if date_from:
        params.append(("entry_date", f"gte.{date_from}"))
    if date_to:
        params.append(("entry_date", f"lte.{date_to}"))
    if mode:
        params.append(("mode", f"eq.{mode}"))
    if direction:
        params.append(("direction", f"eq.{direction}"))
    if status:
        params.append(("status", f"eq.{status}"))
    if q:
        safe = q.replace(",", " ").replace("(", " ").replace(")", " ").replace("*", " ")
        params.append(("or", f"(purpose.ilike.*{safe}*,party.ilike.*{safe}*,notes.ilike.*{safe}*,category.ilike.*{safe}*)"))
    r = await _c().get(f"/{TABLE}", params=params)
    r.raise_for_status()
    return r.json()


async def list_all(**filters) -> list[dict]:
    """Every matching row. Supabase returns at most 1000 rows per request, so page through them."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = await list_entries(limit=1000, offset=offset, **filters)
        rows += page
        if len(page) < 1000:
            return rows
        offset += 1000


async def last_created() -> Optional[dict]:
    rows = await list_entries(limit=1, order="created_at.desc")
    return rows[0] if rows else None
