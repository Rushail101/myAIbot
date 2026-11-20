"""Telegram bot for the Nova Geo Resources cashbook.

Flow
  * Send a UPI screenshot  -> bot reads amount/payee/date, asks what it was for.
    Tap a category button, or just reply to that message with free text.
  * /cash 500 diesel       -> cash paid out.     /cashin 2000 partner top-up -> cash received.
  * /today /pending /summary /total /undo
  * Once a payment is labelled the bot asks "Do you have a bill for this?".
    Yes -> send the bill photo(s) or PDF; they're stored with that entry.
    You can also attach a bill later by replying to the entry's message with a photo.
"""
import asyncio
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import pytesseract
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import db, ocr

log = logging.getLogger("cashbook")
OCR_TIMEOUT = float(os.environ.get("OCR_TIMEOUT", "90"))  # seconds before giving up on reading a screenshot
OWNER_ID = int(os.environ.get("TELEGRAM_OWNER_ID", "0"))
IST = timezone(timedelta(hours=5, minutes=30))
CATEGORIES = [
    c.strip()
    for c in os.environ.get(
        "CATEGORIES",
        "Raw material,Labour,Diesel/Fuel,Electricity,Transport,Repairs,Food/Tea,Partner,Other",
    ).split(",")
    if c.strip()
]


# ---------- helpers ----------
def today_ist():
    return datetime.now(IST).date()


def inr(x) -> str:
    """Indian digit grouping: 1234567.5 -> ₹12,34,567.50"""
    if x is None:
        return "₹?"
    x = float(x)
    whole, _, frac = f"{abs(x):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        whole = f"{head},{tail}"
    s = f"₹{whole}" + (f".{frac}" if frac != "00" else "")
    return ("-" if x < 0 else "") + s


def parse_amount_text(s: str):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_MONTHS = {m: i + 1 for i, m in enumerate(_MON.split("|"))}
_DATE_RES = [
    re.compile(r"\b(?:on\s+)?(yesterday|today)\b", re.I),
    re.compile(r"\b(?:on\s+)?(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?(?!\d)", re.I),   # 8/10, 08-10-2026
    re.compile(rf"\b(?:on\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s*({_MON})[a-z]*\.?(?:\s+(\d{{4}}))?\b", re.I),  # 8 oct
    re.compile(rf"\b(?:on\s+)?({_MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I),  # oct 8
]


def extract_date(text: str):
    """Pulls a date out of free text. Returns (date or None, text with the date removed)."""
    today = today_ist()
    for i, rx in enumerate(_DATE_RES):
        m = rx.search(text)
        if not m:
            continue
        try:
            if i == 0:
                d = today - timedelta(days=1) if m.group(1).lower() == "yesterday" else today
            else:
                if i == 1:
                    day, mon, yr = int(m.group(1)), int(m.group(2)), m.group(3)
                elif i == 2:
                    day, mon, yr = int(m.group(1)), _MONTHS[m.group(2).lower()], m.group(3)
                else:
                    day, mon, yr = int(m.group(2)), _MONTHS[m.group(1).lower()], None
                year = int(yr) + (2000 if yr and len(yr) == 2 else 0) if yr else today.year
                d = today.replace(year=year, month=mon, day=day)
                if not yr and d > today:  # "28 dec" typed in January means last year
                    d = d.replace(year=d.year - 1)
        except ValueError:
            continue
        if d > today:
            continue
        return d, (text[: m.start()] + " " + text[m.end():]).strip()
    return None, text


def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != OWNER_ID:
            if update.effective_message:
                await update.effective_message.reply_text(f"Private bot. Your Telegram id is {user.id if user else '?'}.")
            return
        return await fn(update, ctx)

    return wrapper


def arrow(direction: str) -> str:
    return "⬇️ received" if direction == "in" else "⬆️ paid"


def describe(e: dict) -> str:
    when = e["entry_date"] + (f" {e['entry_time'][:5]}" if e.get("entry_time") else "")
    bits = [f"{inr(e.get('amount'))} {arrow(e['direction'])}", when, e["mode"].upper()]
    if e.get("party"):
        bits.append(e["party"])
    line = " · ".join(bits)
    if e.get("purpose"):
        line += f"\n📝 {e['purpose']}"
    n = len(e.get("bill_paths") or [])
    if n:
        line += f"\n📎 {n} bill page{'s' if n > 1 else ''}"
    return line


def entry_keyboard(e: dict) -> InlineKeyboardMarkup:
    eid = e["id"]
    cats = [InlineKeyboardButton(c, callback_data=f"cat|{eid}|{i}") for i, c in enumerate(CATEGORIES)]
    rows = [cats[i : i + 3] for i in range(0, len(cats), 3)]
    rows.append(
        [
            InlineKeyboardButton("✏️ Amount", callback_data=f"fix|{eid}"),
            InlineKeyboardButton("🔄 In/Out", callback_data=f"flip|{eid}"),
            InlineKeyboardButton("🗑 Discard", callback_data=f"del|{eid}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def pending_count() -> int:
    return len(await db.list_entries(status="pending", limit=500))


async def finish(e: dict, purpose: str, category: str | None = None) -> dict:
    return await db.update(e["id"], {"purpose": purpose, "category": category or e.get("category"), "status": "done"})


# ---------- bills ----------
BILL_WAIT_SECONDS = 15 * 60  # after tapping "Yes", photos count as bills for this long
BILL_QUESTION = "\n\nDo you have a bill for this?"
STOP_WORDS = {"no", "skip", "done", "cancel", "nahi"}


def bill_keyboard(eid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📎 Yes, add bill", callback_data=f"bill|{eid}|y"),
          InlineKeyboardButton("No", callback_data=f"bill|{eid}|n")]]
    )


def done_keyboard(eid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data=f"bill|{eid}|done")]])


def wants_bill(e: dict) -> bool:
    """Ask about a bill only for money paid out that doesn't have one yet."""
    return e.get("direction") == "out" and not e.get("bill_paths")


def bill_target_id(ctx: ContextTypes.DEFAULT_TYPE):
    """The entry we're collecting bill photos for, if 'Yes' was tapped recently."""
    w = ctx.user_data.get("bill_for")
    if not w:
        return None
    if time.time() - w["at"] > BILL_WAIT_SECONDS:
        ctx.user_data.pop("bill_for", None)
        return None
    return w["id"]


def start_bill_wait(ctx: ContextTypes.DEFAULT_TYPE, eid: int):
    ctx.user_data["bill_for"] = {"id": eid, "at": time.time()}


def labelled_reply(e: dict, left: int | None = None):
    """Text + buttons shown once an entry is labelled: the summary, then the bill question."""
    text = "✅ " + describe(e)
    if left is not None:
        text += f"\n\n⏳ {left} more pending" if left else "\n\nAll caught up 🎉"
    if wants_bill(e):
        return text + BILL_QUESTION, bill_keyboard(e["id"])
    return text, None


BILL_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "application/pdf": "pdf"}


async def save_bill(msg, e: dict, ctx: ContextTypes.DEFAULT_TYPE):
    """Downloads the photo/PDF in msg and stores it as a bill page of entry e."""
    if msg.photo:
        tg_file, ctype = await msg.photo[-1].get_file(), "image/jpeg"
    else:
        ctype = msg.document.mime_type or "application/octet-stream"
        tg_file = await msg.document.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    try:
        e = await db.add_bill(e["id"], data, BILL_EXT.get(ctype, "jpg"), ctype)
    except Exception as exc:
        log.exception("saving bill failed")
        await msg.reply_text(
            f"⚠️ Couldn't save the bill ({exc.__class__.__name__}). Send it again, or check /status."
        )
        return
    start_bill_wait(ctx, e["id"])  # keep collecting pages for this entry
    sent = await msg.reply_text(
        "📎 Bill saved.\n" + describe(e) + "\n\nMore pages? Send them now. Otherwise tap Done.",
        reply_markup=done_keyboard(e["id"]),
    )
    await db.update(e["id"], {"tg_msg_id": sent.message_id})  # replying to this later also adds pages


# ---------- commands ----------
@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Nova Geo cashbook 📒\n\n"
        "• Send a UPI screenshot — I read the amount, you tell me what it was for "
        "(tap a category, or reply to my message with text).\n"
        "• /cash 500 diesel — cash paid\n"
        "• /cashin 2000 partner top-up — cash received\n"
        "• /today — today's entries\n"
        "• /pending — entries still missing a purpose\n"
        "• /summary — this month's totals\n"
        "• /total — all-time totals (or /total sep for one month)\n"
        "• /undo — delete the last entry\n"
        "• /status — check that the bot, reader and database are working\n"
        "• Bills — after each payment I ask if you have a bill. Tap Yes and send the photo or PDF. "
        "To add one later, reply to that entry's message with the photo.\n\n"
        "Tip: send many screenshots in a row and name them later — each one is tracked separately. "
        "Reply to a specific screenshot message to label that one."
    )


async def _log_cash(update: Update, ctx: ContextTypes.DEFAULT_TYPE, direction: str):
    text = " ".join(ctx.args or [])
    when, text = extract_date(text)  # "yesterday", "8 oct", "8/10" anywhere in the message
    amount = parse_amount_text(text)
    if amount is None:
        cmd = "cashin" if direction == "in" else "cash"
        await update.message.reply_text(f"Usage: /{cmd} 500 what it was for  (add a date like 'yesterday' or '8 oct' if needed)")
        return
    purpose = re.sub(r"^\s*\d[\d,]*(?:\.\d+)?\s*", "", text).strip()
    e = await db.insert(
        {
            "entry_date": (when or today_ist()).isoformat(),
            "entry_time": None if when and when != today_ist() else datetime.now(IST).strftime("%H:%M"),
            "amount": amount,
            "direction": direction,
            "mode": "cash",
            "purpose": purpose or None,
            "status": "done" if purpose else "pending",
        }
    )
    ctx.user_data.pop("bill_for", None)  # a new entry ends any bill collection for the previous one
    if purpose:
        text, kb = labelled_reply(e)
        sent = await update.message.reply_text(text, reply_markup=kb)
        await db.update(e["id"], {"tg_msg_id": sent.message_id})
    else:
        sent = await update.message.reply_text(
            f"{describe(e)}\nWhat was it for?", reply_markup=entry_keyboard(e)
        )
        await db.update(e["id"], {"tg_msg_id": sent.message_id})


@owner_only
async def cmd_cash(update, ctx):
    await _log_cash(update, ctx, "out")


@owner_only
async def cmd_cashin(update, ctx):
    await _log_cash(update, ctx, "in")


@owner_only
async def cmd_today(update, ctx):
    rows = await db.list_entries(date_from=today_ist().isoformat(), date_to=today_ist().isoformat(), order="id.asc")
    if not rows:
        await update.message.reply_text("Nothing logged today yet.")
        return
    out = sum(float(r["amount"] or 0) for r in rows if r["direction"] == "out")
    inn = sum(float(r["amount"] or 0) for r in rows if r["direction"] == "in")
    lines = [
        f"{'⏳' if r['status']=='pending' else '•'} {inr(r['amount'])} {'in' if r['direction']=='in' else 'out'} "
        f"({r['mode']}) — {r.get('purpose') or r.get('party') or 'no purpose yet'}"
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines) + f"\n\nIn {inr(inn)} · Out {inr(out)} · Net {inr(inn - out)}")


@owner_only
async def cmd_pending(update, ctx):
    rows = await db.list_entries(status="pending", order="id.asc", limit=30)
    if not rows:
        await update.message.reply_text("All caught up ✅")
        return
    await update.message.reply_text(f"{len(rows)} entr{'y' if len(rows)==1 else 'ies'} need a purpose. Sending them one by one:")
    for r in rows:
        sent = await update.message.reply_text(describe(r) + "\nWhat was it for?", reply_markup=entry_keyboard(r))
        await db.update(r["id"], {"tg_msg_id": sent.message_id})


@owner_only
async def cmd_summary(update, ctx):
    first = today_ist().replace(day=1).isoformat()
    rows = await db.list_all(date_from=first, select="amount,direction,category")
    out = sum(float(r["amount"] or 0) for r in rows if r["direction"] == "out")
    inn = sum(float(r["amount"] or 0) for r in rows if r["direction"] == "in")
    by_cat: dict[str, float] = {}
    for r in rows:
        if r["direction"] == "out":
            k = r.get("category") or "Uncategorised"
            by_cat[k] = by_cat.get(k, 0) + float(r["amount"] or 0)
    top = "\n".join(f"  {k}: {inr(v)}" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])[:8])
    await update.message.reply_text(
        f"This month ({len(rows)} entries)\nIn {inr(inn)} · Out {inr(out)} · Net {inr(inn - out)}\n\nSpend by category:\n{top or '  —'}"
    )


@owner_only
async def cmd_total(update, ctx):
    """/total = everything ever.  /total sep  or  /total sep 2025 = one month."""
    arg = " ".join(ctx.args or []).strip().lower()
    date_from = date_to = None
    label = "All time"
    if arg:
        m = re.match(rf"^({_MON})[a-z]*\.?(?:\s+(\d{{4}}))?$", arg)
        if not m:
            await update.message.reply_text("Usage: /total for everything, or /total sep (a month), or /total sep 2025")
            return
        mon, today = _MONTHS[m.group(1)], today_ist()
        year = int(m.group(2)) if m.group(2) else (today.year if mon <= today.month else today.year - 1)
        first = date(year, mon, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        date_from, date_to, label = first.isoformat(), last.isoformat(), first.strftime("%B %Y")

    rows = await db.list_all(date_from=date_from, date_to=date_to, select="amount,direction,mode,category,status")
    if not rows:
        await update.message.reply_text(f"{label}: no entries.")
        return

    def tot(direction, mode=None):
        return sum(float(r["amount"] or 0) for r in rows if r["direction"] == direction and (mode is None or r["mode"] == mode))

    inn, out = tot("in"), tot("out")
    by_cat: dict[str, float] = {}
    for r in rows:
        if r["direction"] == "out":
            k = r.get("category") or "Uncategorised"
            by_cat[k] = by_cat.get(k, 0) + float(r["amount"] or 0)
    cats = "\n".join(f"  {k}: {inr(v)}" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])[:10])
    pending = sum(1 for r in rows if r["status"] == "pending")
    await update.message.reply_text(
        f"📊 {label} ({len(rows)} entries)\n"
        f"In {inr(inn)} · Out {inr(out)} · Net {inr(inn - out)}\n\n"
        f"UPI:  in {inr(tot('in', 'upi'))} · out {inr(tot('out', 'upi'))}\n"
        f"Cash: in {inr(tot('in', 'cash'))} · out {inr(tot('out', 'cash'))} · net {inr(tot('in', 'cash') - tot('out', 'cash'))}\n\n"
        f"Spend by category:\n{cats or '  —'}"
        + (f"\n\n⏳ {pending} entr{'y' if pending == 1 else 'ies'} still need a purpose" if pending else "")
    )


@owner_only
async def cmd_undo(update, ctx):
    e = await db.last_created()
    if not e:
        await update.message.reply_text("Nothing to undo.")
        return
    await db.delete(e["id"])
    await update.message.reply_text("🗑 Deleted:\n" + describe(e))


# ---------- screenshots ----------
@owner_only
async def on_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    is_pdf = bool(msg.document and msg.document.mime_type == "application/pdf")

    # Is this a bill? Either a reply to an entry's message, or sent after tapping "Yes, add bill".
    bill_entry = None
    if msg.reply_to_message:
        bill_entry = await db.get_by_msg(msg.reply_to_message.message_id)
    if bill_entry is None and bill_target_id(ctx):
        bill_entry = await db.get(bill_target_id(ctx))
    if bill_entry is not None:
        await save_bill(msg, bill_entry, ctx)
        return
    if is_pdf:
        await msg.reply_text(
            "PDFs are saved as bills only. Tap “📎 Yes, add bill” on an entry first, "
            "or reply to that entry's message with the PDF."
        )
        return

    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        tg_file = await msg.document.get_file()
    else:
        return
    ack = await msg.reply_text("📸 Got it, reading…")  # instant feedback while OCR runs
    try:
        data = bytes(await tg_file.download_as_bytearray())
        try:
            info = await asyncio.wait_for(asyncio.to_thread(ocr.extract, data), timeout=OCR_TIMEOUT)
        except Exception as exc:  # OCR failure/timeout shouldn't lose the transaction
            log.warning("OCR failed or timed out: %r", exc)
            info = {"amount": None, "direction": "out", "party": None, "entry_date": None, "upi_ref": None}

        ref = info.get("upi_ref")
        if ref:
            dup = await db.get_by_ref(ref)
            if dup:
                await ack.edit_text("⚠️ Looks like a duplicate — already logged:\n" + describe(dup))
                return

        e = await db.insert(
            {
                "entry_date": (info.get("entry_date") or today_ist()).isoformat(),
                "entry_time": info.get("entry_time"),
                "amount": info.get("amount"),
                "direction": info.get("direction") or "out",
                "mode": "upi",
                "party": info.get("party"),
                "upi_ref": ref,
                "status": "pending",
                "tg_msg_id": ack.message_id,  # the message we edit below, so replying to it labels this entry
            }
        )
        if e.get("amount") is None:
            await ack.edit_text(
                "Couldn't read the amount. Reply to this message with the amount (e.g. 1500).",
                reply_markup=entry_keyboard(e),
            )
            ctx.user_data["fix"] = e["id"]
        else:
            warn = "" if info.get("amount_sure", True) else "\n⚠️ Please double-check the amount (tap ✏️ Amount to fix)"
            await ack.edit_text(describe(e) + warn + "\nWhat was it for?", reply_markup=entry_keyboard(e))
    except Exception as exc:
        log.exception("on_image failed")
        try:  # never leave the message stuck on "reading…"
            await ack.edit_text(
                f"⚠️ Couldn't save that one ({exc.__class__.__name__}). Please send it again, "
                "or check /status. If it was already saved you'll get a duplicate notice."
            )
        except Exception:
            pass


@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["✅ Bot is running"]
    try:
        ver = await asyncio.to_thread(pytesseract.get_tesseract_version)
        lines.append(f"✅ Screenshot reader ready (Tesseract {ver})")
    except Exception as exc:
        lines.append(f"❌ Screenshot reader problem: {exc.__class__.__name__}")
    try:
        await db.list_entries(limit=1)
        lines.append("✅ Database reachable")
    except Exception as exc:
        lines.append(f"❌ Database problem: {exc.__class__.__name__}")
    await update.message.reply_text("\n".join(lines))


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error while handling an update", exc_info=ctx.error)


# ---------- free text ----------
@owner_only
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = (msg.text or "").strip()
    if not text:
        return

    # "no" / "done" while the bot is waiting for bill photos
    if bill_target_id(ctx) and text.lower().rstrip(".!") in STOP_WORDS:
        ctx.user_data.pop("bill_for", None)
        await msg.reply_text("OK 👍")
        return

    # "cash 500 diesel" typed without the slash
    m = re.match(r"^(cash ?in|cash)\s+(.*)$", text, re.I)
    if m:
        ctx.args = m.group(2).split()
        await _log_cash(update, ctx, "in" if "in" in m.group(1).lower() else "out")
        return

    target = None
    if msg.reply_to_message:
        target = await db.get_by_msg(msg.reply_to_message.message_id)
    if target is None and ctx.user_data.get("fix"):
        target = await db.get(ctx.user_data["fix"])
        if target:
            amt = parse_amount_text(text)
            if amt is None:
                await msg.reply_text("Send just the amount, e.g. 1500")
                return
            ctx.user_data.pop("fix", None)
            e = await db.update(target["id"], {"amount": amt})
            await msg.reply_text(describe(e) + "\nWhat was it for?", reply_markup=entry_keyboard(e))
            return
    if target is None:
        pend = await db.list_entries(status="pending", order="id.desc", limit=1)
        target = pend[0] if pend else None
    if target is None:
        await msg.reply_text("No entry waiting for a label. Send a UPI screenshot or use /cash 500 reason.")
        return

    if target.get("amount") is None:
        amt = parse_amount_text(text)
        if amt is None:
            await msg.reply_text("I still need the amount for that one — send just the number.")
            return
        e = await db.update(target["id"], {"amount": amt})
        await msg.reply_text(describe(e) + "\nWhat was it for?", reply_markup=entry_keyboard(e))
        return

    e = await finish(target, text)
    reply, kb = labelled_reply(e, await pending_count())
    sent = await msg.reply_text(reply, reply_markup=kb)
    if kb:
        await db.update(e["id"], {"tg_msg_id": sent.message_id})


# ---------- button taps ----------
@owner_only
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind, eid, *rest = q.data.split("|")
    eid = int(eid)
    e = await db.get(eid)
    if e is None:
        await q.edit_message_text("That entry no longer exists.")
        return

    if kind == "cat":
        cat = CATEGORIES[int(rest[0])]
        if e.get("amount") is None:
            await q.message.reply_text("Set the amount first (✏️ Amount).")
            return
        e = await finish(e, cat, cat)
        text, kb = labelled_reply(e, await pending_count())
        await q.edit_message_text(text, reply_markup=kb)
    elif kind == "bill":
        choice = rest[0] if rest else "n"
        if choice == "y":
            start_bill_wait(ctx, eid)
            await q.edit_message_text(
                "✅ " + describe(e) + "\n\n📎 Send the bill photo now (a PDF works too). "
                "Several pages? Send them one after another."
            )
        else:  # "n" or "done"
            if ctx.user_data.get("bill_for", {}).get("id") == eid:
                ctx.user_data.pop("bill_for", None)
            await q.edit_message_text("✅ " + describe(e))
    elif kind == "fix":
        ctx.user_data["fix"] = eid
        await db.update(eid, {"tg_msg_id": q.message.message_id})
        await q.message.reply_text("Send the correct amount (number only).")
    elif kind == "flip":
        e = await db.update(eid, {"direction": "in" if e["direction"] == "out" else "out"})
        await q.edit_message_text(describe(e) + "\nWhat was it for?", reply_markup=entry_keyboard(e))
    elif kind == "del":
        await db.delete(eid)
        await q.edit_message_text("🗑 Discarded.")


def build_application() -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("cash", cmd_cash))
    app.add_handler(CommandHandler("cashin", cmd_cashin))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("total", cmd_total))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_error_handler(on_error)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
