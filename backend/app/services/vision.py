"""Document recognition: page images in, one fixed JSON shape out.

The model is reached through any OpenAI-compatible chat endpoint that accepts images
(the company model gateway, Qwen-VL, a local Ollama...), configured by VISION_* settings.
Nothing configured = recognition is off and documents are keyed in by hand on the review
screen. Tests swap in their own extractor with `set_extractor`.
"""

import base64
import io
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from app.core.config import get_settings

MAX_PAGES = 5
MAX_SIDE = 2000  # px; enough for receipts, keeps requests small

SCHEMA = {
    "kind": "INVOICE | CN | PAYMENT | BANK | RETURN_NOTE | OTHER",
    "supplier_name": "string or null",
    "supplier_tin": "string or null (Malaysian TIN, e.g. C1234567890)",
    "doc_no": "the document's own number, or null",
    "doc_date": "YYYY-MM-DD or null (Malaysian dates are day/month/year)",
    "ref_invoice_no": "CN only: the invoice number it refers to, or null",
    "currency": "MYR unless shown otherwise",
    "subtotal": "number or null",
    "tax_total": "number or null (SST)",
    "total": "number or null",
    "lines": [
        {
            "barcode": "string or null",
            "description": "string",
            "qty": "number",
            "uom": "unit as printed (PCS, CTN, PKT, KG...) or null",
            "unit_price": "number or null",
            "discount_pct": "number or null",
            "amount": "line amount as printed, number or null",
            "sst": "true if the line is marked as taxed",
        }
    ],
    "payment_lines": [
        {
            "line_type": "INVOICE (paid) | CN (deducted) | FEE (deducted) | OTHER",
            "ref_no": "invoice / CN number as printed, or null",
            "amount": "number, always positive",
            "fee_type": "FREIGHT | LISTING | PROMOTION | DISPLAY | REBATE | OTHER, or null",
            "description": "string or null",
        }
    ],
    "bank": {
        "date": "YYYY-MM-DD or null",
        "amount": "number or null",
        "direction": "IN | OUT",
        "counterparty": "string or null",
        "reference": "string or null",
    },
    "uncertain": ["json paths you are not sure about, e.g. lines.2.qty"],
}

PROMPT = (
    "You read Malaysian supplier documents for a retail shop: invoices, credit notes (CN), "
    "payment advices / statements, bank transfer slips, and handwritten return notes. "
    "Return ONLY one JSON object with exactly this shape (use null for anything not shown, "
    "never guess numbers, copy numbers and codes exactly as printed):\n"
    + json.dumps(SCHEMA, indent=1)
)


class ExtractionError(Exception):
    pass


@dataclass
class Extracted:
    data: dict[str, Any]
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    uncertain: list[str] = field(default_factory=list)


class Extractor(Protocol):
    name: str

    def extract(self, pages: list[tuple[bytes, str]], hint: str | None) -> Extracted: ...


# --- page images --------------------------------------------------------------------------


def to_pages(content: bytes, content_type: str) -> list[tuple[bytes, str]]:
    """PDF -> one PNG per page (first MAX_PAGES); large photos are scaled down."""
    from PIL import Image

    if content_type == "application/pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(content)
        images = [pdf[i].render(scale=2).to_pil() for i in range(min(len(pdf), MAX_PAGES))]
    else:
        images = [Image.open(io.BytesIO(content))]
    out = []
    for img in images:
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        out.append((buf.getvalue(), "image/jpeg"))
    return out


# --- OpenAI-compatible provider -----------------------------------------------------------


class OpenAICompatible:
    name = "vision"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float) -> None:
        self.base_url, self.api_key, self.model, self.timeout = (
            base_url.rstrip("/"),
            api_key,
            model,
            timeout,
        )

    def extract(self, pages: list[tuple[bytes, str]], hint: str | None) -> Extracted:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": f"Document type hint from the user: {hint or 'unknown'}"}
        ]
        for data, mime in pages:
            b64 = base64.b64encode(data).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": content},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        started = time.monotonic()
        try:
            r = httpx.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
            r.raise_for_status()
            reply = r.json()
            text = reply["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise ExtractionError(f"Recognition service failed: {exc}") from exc
        usage = reply.get("usage") or {}
        data = parse_json(text)
        return Extracted(
            data=data,
            model=self.model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            seconds=time.monotonic() - started,
            uncertain=[str(x) for x in data.pop("uncertain", None) or []],
        )


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ExtractionError("Recognition did not return JSON") from exc
    if not isinstance(data, dict):
        raise ExtractionError("Recognition did not return a JSON object")
    return data


_override: Extractor | None = None
_overridden = False


def set_extractor(extractor: Extractor | None) -> None:
    """Replace the configured extractor (tests); set_extractor(None) turns recognition off."""
    global _override, _overridden
    _override, _overridden = extractor, True


def reset_extractor() -> None:
    global _override, _overridden
    _override, _overridden = None, False


def get_extractor() -> Extractor | None:
    if _overridden:
        return _override
    s = get_settings()
    if not (s.vision_base_url and s.vision_model):
        return None
    return OpenAICompatible(
        s.vision_base_url, s.vision_api_key, s.vision_model, s.vision_timeout_seconds
    )


# --- cleaning what the model (or a person) typed ------------------------------------------


def num(v: Any) -> Decimal | None:
    """'1,234.50', 'RM 12.00', 12 -> Decimal; anything else -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float | Decimal):
        return Decimal(str(v))
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def day(v: Any) -> date | None:
    """ISO dates, or day/month/year as printed in Malaysia (31/12/2026, 31-12-26)."""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", s)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
