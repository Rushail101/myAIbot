"""Runs the Telegram bot (long polling) and the dashboard API in one process."""
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import bot as botmod
from . import db

DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")
STATIC = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tg = botmod.build_application()
    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling(drop_pending_updates=False)  # keep screenshots sent while a redeploy was happening
    try:
        yield
    finally:
        await tg.updater.stop()
        await tg.stop()
        await tg.shutdown()


app = FastAPI(title="Nova Geo Cashbook", lifespan=lifespan)


def auth(x_api_key: Optional[str] = Header(default=None)):
    """Single-user app, so no accounts. If DASHBOARD_KEY is set, requests must carry it."""
    if DASHBOARD_KEY and not hmac.compare_digest(x_api_key or "", DASHBOARD_KEY):
        raise HTTPException(401, "Bad or missing key")


class EntryIn(BaseModel):
    entry_date: str
    entry_time: Optional[str] = None
    amount: float
    direction: str = "out"
    mode: str = "cash"
    purpose: Optional[str] = None
    category: Optional[str] = None
    party: Optional[str] = None
    upi_ref: Optional[str] = None
    notes: Optional[str] = None


class EntryPatch(BaseModel):
    entry_date: Optional[str] = None
    entry_time: Optional[str] = None
    amount: Optional[float] = None
    direction: Optional[str] = None
    mode: Optional[str] = None
    purpose: Optional[str] = None
    category: Optional[str] = None
    party: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/entries", dependencies=[Depends(auth)])
async def list_entries(
    date_from: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    mode: Optional[str] = None,
    direction: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
):
    return await db.list_all(date_from=date_from, date_to=to, mode=mode, direction=direction, status=status, q=q)


@app.post("/api/entries", dependencies=[Depends(auth)])
async def create_entry(body: EntryIn):
    row = body.model_dump()
    row["status"] = "done" if (row.get("purpose") or "").strip() else "pending"
    row["upi_ref"] = row["upi_ref"] or None
    return await db.insert(row)


@app.patch("/api/entries/{entry_id}", dependencies=[Depends(auth)])
async def patch_entry(entry_id: int, body: EntryPatch):
    fields = body.model_dump(exclude_unset=True)
    if "purpose" in fields and "status" not in fields:
        fields["status"] = "done" if (fields["purpose"] or "").strip() else "pending"
    if not fields:
        raise HTTPException(400, "Nothing to update")
    return await db.update(entry_id, fields)


@app.delete("/api/entries/{entry_id}", dependencies=[Depends(auth)])
async def remove_entry(entry_id: int):
    await db.delete(entry_id)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
