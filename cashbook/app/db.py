"""Thin async wrapper over Supabase's REST API (PostgREST). No extra SDK needed."""
import os
from typing import Any, Optional

import httpx

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TABLE = "cashbook_entries"

_client: Optional[httpx.AsyncClient] = None


def _c() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=f"{URL}/rest/v1",
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            timeout=20,
        )
    return _client


async def insert(row: dict) -> dict:
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
    r = await _c().delete(f"/{TABLE}", params={"id": f"eq.{entry_id}"})
    r.raise_for_status()


async def list_entries(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    mode: Optional[str] = None,
    direction: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 1000,
    order: str = "entry_date.desc,id.desc",
) -> list[dict]:
    params: list[tuple[str, Any]] = [("order", order), ("limit", limit)]
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


async def last_created() -> Optional[dict]:
    rows = await list_entries(limit=1, order="created_at.desc")
    return rows[0] if rows else None
