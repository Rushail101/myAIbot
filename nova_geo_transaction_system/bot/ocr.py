"""Local OCR and conservative deterministic extraction; no AI API calls."""
import re
from datetime import datetime
from PIL import Image
import pytesseract

def ocr_image(path: str) -> str:
    return pytesseract.image_to_string(Image.open(path).convert("RGB"), config="--psm 6")

def parse_payment_screenshot(path: str) -> dict:
    text = ocr_image(path)
    amount = None
    for pattern in (r"(?:₹|INR\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", r"(?:paid|amount)\s*(?:of\s*)?(?:₹|INR)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"):
        m = re.search(pattern, text, re.I)
        if m:
            try:
                amount = round(float(m.group(1).replace(",", "")), 2)
                if amount <= 0: amount = None
                break
            except ValueError: pass
    ref = None
    m = re.search(r"(?:UPI\s*(?:Ref(?:erence)?(?:\s*No)?|ID)|Transaction\s*(?:ID|Ref(?:erence)?|No\.?))\s*[:#\-]?\s*([A-Z0-9]{8,25})", text, re.I)
    if m: ref = m.group(1).strip()
    lines = [re.sub(r"\s+", " ", x).strip(" :-") for x in text.splitlines() if x.strip()]
    payee = None
    for line in lines:
        m = re.match(r"to\s+(.+)", line, re.I)
        if m and len(m.group(1)) > 2:
            payee = m.group(1)[:100]; break
    if not payee:
        skip = re.compile(r"(successful|success|paid|payment|upi|reference|transaction|date|time|from|to|debited|super\.money|phonepe|google pay|gpay|paytm|₹|inr)", re.I)
        for line in lines:
            if 3 < len(line) < 90 and not skip.search(line) and not re.search(r"\d{6,}", line):
                payee = line[:100]; break
    status = "successful" if re.search(r"\b(successful|successfully paid|payment successful|completed)\b", text, re.I) else "unknown"
    return {"raw_text": text, "amount": amount, "payee": payee, "upi_reference": ref,
            "payment_status": status, "payment_method": "UPI", "transaction_date": datetime.now().date().isoformat(),
            "source": "telegram_screenshot"}
