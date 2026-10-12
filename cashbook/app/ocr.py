"""Reads a UPI payment screenshot (GPay / PhonePe / Paytm / bank apps) with Tesseract.

No AI API is used. Everything here is plain OCR + regex, so the bot always asks you
to confirm the amount before it is trusted.
"""
import io
import re
from datetime import date, datetime, timedelta
from typing import Optional

import pytesseract
from dateutil import parser as dateparser
from PIL import Image, ImageOps, ImageStat

NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
CUR_AMOUNT = re.compile(rf"(?<![A-Za-z])(?:₹|Rs\.?|INR)\s*({NUM})(?![\d,])", re.I)
# Tesseract often turns the ₹ glyph into X, %, T, Z, F, € ... so allow one stray leading character
LONE_AMOUNT = re.compile(rf"^[^\d\s]?\s?({NUM})\s*$")
# last resort: same, but tolerate 1-2 junk chars after it (the green tick beside the amount is sometimes read as "a" or "}")
LONE_AMOUNT_LOOSE = re.compile(rf"^[^\d\s]?\s?({NUM})\s+[^\d\s]{{1,2}}\s*$")

IN_WORDS = re.compile(r"received from|credited|money received|payment received|from\s+\S", re.I)
OUT_WORDS = re.compile(r"paid to|payment to|sent to|debited|money sent|paying", re.I)

PARTY_OUT = re.compile(r"(?:paid to|payment to|sent to|paying|^\s*to)\s*[:\-]?\s*(.+)", re.I | re.M)
PARTY_IN = re.compile(r"(?:received from|credited by|^\s*from)\s*[:\-]?\s*(.+)", re.I | re.M)

DATE_PATTERNS = [
    re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}\b"),   # 10 Oct 2026
    re.compile(r"\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b"),   # Oct 10, 2026
    re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b"),      # 10/10/2026
]
MONTH_WORD = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b",
    re.I,
)
_M = MONTH_WORD.pattern[3:-3]  # the alternation without the \b( ) wrapper
DATE_NO_YEAR = [
    re.compile(rf"\b(?:{_M})\.?[ \t]+\d{{1,2}}(?!\d)", re.I),    # October 10
    re.compile(rf"\b\d{{1,2}}[ \t]+(?:{_M})\b", re.I),           # 10 Oct
]
TIME_12H = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])\b")
TIME_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b")
# 12 digits standing alone - not part of an id like "vyapar.175693365847@hdfcbank" or "name123456789012@ybl"
REF_12 = re.compile(r"(?<![\w@.\-])(\d{12})(?![\w@])")
REF_LABELLED_12 = re.compile(r"(?:reference|ref|utr|transaction)[^\n\d]{0,20}?(\d{12})(?!\d)", re.I)
REF_LABEL = re.compile(
    r"(?:UPI\s*(?:Ref(?:erence)?|transaction)?\s*(?:No|ID|Id)?\.?|Transaction\s*ID|UTR|Ref(?:erence)?\s*(?:No|ID)?\.?)"
    r"\s*[:#]?\s*([A-Za-z0-9]{10,})",
    re.I,
)


def _prepare(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("L")
    if img.width < 1100:
        scale = 1100 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    if ImageStat.Stat(img).mean[0] < 110:  # dark-mode screenshot -> invert
        img = ImageOps.invert(img)
    return ImageOps.autocontrast(img)


def _to_float(s: str) -> Optional[float]:
    try:
        v = float(s.replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def parse_amount_ex(text: str, loose: bool = True):
    """Returns (amount, sure). sure=True when a rupee sign / symbol stood in front of the number,
    so the first digit can't be a misread rupee glyph."""
    m = CUR_AMOUNT.search(text)
    if m and len(re.sub(r"\D", "", m.group(1))) < 9:
        v = _to_float(m.group(1))
        if v:
            return v, True
    for rx, strict in ((LONE_AMOUNT, True), (LONE_AMOUNT_LOOSE, False)):
        if not strict and not loose:
            continue
        for line in text.splitlines():
            line = line.strip()
            m = rx.match(line)
            if not m:
                continue
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) >= 9:  # looks like a reference id, not money
                continue
            # a bare 4-digit year on its own line (never "2,000", which is a formatted amount)
            if re.fullmatch(r"20[2-3]\d", m.group(1)):
                continue
            # the loose pass is only trusted for clearly money-shaped text, never "12 PM"-style fragments
            if not strict and not ("," in m.group(1) or len(digits) >= 3):
                continue
            v = _to_float(m.group(1))
            if v:
                return v, not line[0].isdigit()
    return None, False


def parse_amount(text: str, loose: bool = True) -> Optional[float]:
    return parse_amount_ex(text, loose)[0]


def parse_direction(text: str) -> str:
    out_m, in_m = OUT_WORDS.search(text), IN_WORDS.search(text)
    if in_m and (not out_m or in_m.start() < out_m.start()):
        if re.search(r"received|credited", text, re.I):
            return "in"
    return "out"


def parse_party(text: str, direction: str) -> Optional[str]:
    pat = PARTY_IN if direction == "in" else PARTY_OUT
    m = pat.search(text)
    if not m:
        return None
    name = re.split(r"[\n|]", m.group(1))[0].strip(" :-")
    # Long business names wrap onto a second ALL-CAPS line (e.g. "...GENERAL" / "STORE")
    nxt = text[m.end():].lstrip("\n").split("\n", 1)[0].strip()
    if name.isupper() and nxt.isupper() and len(nxt) <= 30 and not re.search(r"[\d:@]", nxt):
        name += " " + nxt
    name = re.sub(r"\s{2,}", " ", name)
    return name[:80] or None


def parse_date(text: str) -> Optional[date]:
    today = date.today()
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                d = dateparser.parse(m.group(0), dayfirst=True).date()
            except (ValueError, OverflowError):
                continue
            if today - timedelta(days=730) <= d <= today + timedelta(days=1):
                return d
    # No year printed (e.g. Super.money: "October 10 at 7:08 PM") -> assume this year,
    # or last year if that would land in the future.
    for pat in DATE_NO_YEAR:
        m = pat.search(text)
        if not m:
            continue
        try:
            d = dateparser.parse(m.group(0), default=datetime(today.year, 1, 1)).date()
        except (ValueError, OverflowError):
            continue
        if d > today + timedelta(days=1):
            d = d.replace(year=d.year - 1)
        return d
    return None


def parse_time(text: str) -> Optional[str]:
    """Returns 24h 'HH:MM' or None."""
    m = TIME_12H.search(text)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if 1 <= h <= 12:
            return f"{h % 12 + (12 if ap == 'PM' else 0):02d}:{mi:02d}"
    for line in text.splitlines():  # 24h clock, only on a line that also looks like a date
        if MONTH_WORD.search(line) or re.search(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}", line):
            m = TIME_24H.search(line)
            if m:
                return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def parse_ref(text: str) -> Optional[str]:
    m = REF_LABELLED_12.search(text)  # "UPI reference ID: 628087232375" is the most trustworthy
    if m:
        return m.group(1)
    m = REF_12.search(text)
    if m:
        return m.group(1)
    m = REF_LABEL.search(text)
    return m.group(1) if m else None


def parse_text(text: str, loose: bool = True) -> dict:
    direction = parse_direction(text)
    return {
        "amount": parse_amount(text, loose),
        "direction": direction,
        "party": parse_party(text, direction),
        "entry_date": parse_date(text),
        "entry_time": parse_time(text),
        "upi_ref": parse_ref(text),
    }


def _pick_amount(a, b, a_sure=False, b_sure=False):
    """Combine the amounts found by the two OCR passes. Returns (amount, sure)."""
    if a is None or b is None:
        v = a if a is not None else b
        return v, (a_sure if a is not None else b_sure)
    if a == b:
        return a, a_sure or b_sure
    if a_sure != b_sure:  # one pass saw a symbol in front of the number: believe that one
        return (a, True) if a_sure else (b, True)
    # The rupee sign is sometimes read as a digit (Rs140 -> 2140). If one pass is the other with
    # one extra leading digit, the shorter one is the real amount.
    for longer, shorter in ((a, b), (b, a)):
        ls, ss = f"{longer:g}", f"{shorter:g}"
        if len(ls) == len(ss) + 1 and ls[1:] == ss:
            return shorter, False
    return a, False  # otherwise trust the first pass; the bot flags it for a check


def extract(image_bytes: bytes) -> dict:
    img = _prepare(Image.open(io.BytesIO(image_bytes)))
    text = pytesseract.image_to_string(img, config="--psm 6")
    text2 = pytesseract.image_to_string(img, config="--psm 11")  # sparse mode reads the big amount differently
    info = parse_text(text, loose=False)
    retry = parse_text(text2, loose=False)
    for k, v in retry.items():
        if k != "amount" and info.get(k) is None:
            info[k] = v
    a, a_sure = parse_amount_ex(text, loose=False)
    b, b_sure = parse_amount_ex(text2, loose=False)
    info["amount"], info["amount_sure"] = _pick_amount(a, b, a_sure, b_sure)
    if info["amount"] is None:  # last resort: tolerate junk characters next to the amount
        v, sure = parse_amount_ex(text, loose=True)
        if v is None:
            v, sure = parse_amount_ex(text2, loose=True)
        info["amount"], info["amount_sure"] = v, sure
    info["raw_text"] = text + "\n" + text2
    return info
