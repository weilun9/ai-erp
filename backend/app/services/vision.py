"""Document recognition: page images in, one fixed JSON shape out.

Two engines (VISION_ENGINE = auto | vision | ocr | off):
- vision: any OpenAI-compatible chat endpoint that accepts images (Qwen-VL, GPT-4o-mini,
  Gemini, the company gateway...), configured by VISION_* settings;
- ocr: local OCR (RapidOCR, PaddleOCR models on ONNX Runtime, as in the Doc2Excel app) reads
  the text on this machine, then the text model already used by the butler (LLM_*) turns it
  into the same JSON. No vision key needed; install with `pip install -e ".[local-ocr]"`.
auto = vision when VISION_* is set, else ocr when it is installed and LLM_* is set, else off.
Off = documents are keyed in by hand on the scan screen. Tests swap in their own extractor
with `set_extractor`.
"""

import base64
import io
import json
import re
import statistics
import threading
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


def _chat_json(
    base_url: str, api_key: str, model: str, timeout: float, system: str, content: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One chat call that must answer with a JSON object: (data, usage)."""
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions", json=body, headers=headers, timeout=timeout
        )
        r.raise_for_status()
        reply = r.json()
        text = reply["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise ExtractionError(f"Recognition service failed: {exc}") from exc
    return parse_json(text), reply.get("usage") or {}


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
        started = time.monotonic()
        data, usage = _chat_json(
            self.base_url, self.api_key, self.model, self.timeout, PROMPT, content
        )
        return Extracted(
            data=data,
            model=self.model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            seconds=time.monotonic() - started,
            uncertain=[str(x) for x in data.pop("uncertain", None) or []],
        )


# --- local OCR + text model ---------------------------------------------------------------

OCR_PROMPT = (
    "You read Malaysian supplier documents for a retail shop: invoices, credit notes (CN), "
    "payment advices / statements, bank transfer slips, and handwritten return notes. "
    "You get the OCR text of the paper, one printed line per row, cells separated by ' | '. "
    "OCR can split or merge words and misread characters (0/O, 1/I, 5/S): fix only obvious "
    "slips, and list any value you had to infer in 'uncertain'. "
    "Return ONLY one JSON object with exactly this shape (use null for anything not shown, "
    "never guess numbers, copy numbers and codes exactly as printed):\n"
    + json.dumps(SCHEMA, indent=1)
)
_ocr_engine = None
_ocr_lock = threading.Lock()


def ocr_available() -> bool:
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def _rapid():
    """The RapidOCR engine, loaded once (the first load takes several seconds)."""
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            from rapidocr import RapidOCR

            _ocr_engine = RapidOCR()
        return _ocr_engine


def group_rows(boxes, texts, scores=None) -> list[list[str]]:
    """OCR boxes -> printed lines -> cells. Boxes whose vertical centres are within ~0.6 of
    a text height share a line; a horizontal gap wider than ~1.6 characters starts a new
    cell. (The same layout rules as the Doc2Excel app, so tables keep their columns.)"""
    items = []
    for i, (box, text) in enumerate(zip(boxes, texts, strict=False)):
        text = str(text).strip()
        if not text:
            continue
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        items.append(
            {
                "x0": min(xs),
                "x1": max(xs),
                "yc": (min(ys) + max(ys)) / 2,
                "h": max(ys) - min(ys),
                "t": text + ("?" if scores is not None and scores[i] < 0.6 else ""),
                "cw": (max(xs) - min(xs)) / max(len(text), 1),
            }
        )
    if not items:
        return []
    med_h = statistics.median(i["h"] for i in items) or 10
    items.sort(key=lambda i: i["yc"])
    lines: list[list[dict[str, Any]]] = []
    for it in items:
        if lines:
            cur = lines[-1]
            if abs(it["yc"] - sum(i["yc"] for i in cur) / len(cur)) < 0.6 * med_h:
                cur.append(it)
                continue
        lines.append([it])
    rows = []
    for line in lines:
        line.sort(key=lambda i: i["x0"])
        cells, prev = [line[0]["t"]], line[0]
        for it in line[1:]:
            if it["x0"] - prev["x1"] < 1.6 * max((prev["cw"] + it["cw"]) / 2, 1):
                cells[-1] += " " + it["t"]
            else:
                cells.append(it["t"])
            prev = it
        rows.append(cells)
    return rows


def ocr_text(pages: list[tuple[bytes, str]]) -> str:
    """Page images -> the text, laid out as rows of ' | '-separated cells."""
    import numpy as np
    from PIL import Image

    out = []
    for n, (data, _mime) in enumerate(pages, start=1):
        img = Image.open(io.BytesIO(data)).convert("RGB")
        res = _rapid()(np.array(img))
        boxes = getattr(res, "boxes", None)
        if boxes is None or len(boxes) == 0:
            continue
        rows = group_rows(boxes, res.txts, res.scores)
        if len(pages) > 1:
            out.append(f"--- page {n} ---")
        out.extend(" | ".join(r) for r in rows)
    return "\n".join(out)


class OcrThenText:
    """Local OCR reads the paper; the text model shapes what it read into the JSON."""

    name = "ocr"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float) -> None:
        self.base_url, self.api_key, self.llm, self.timeout = base_url, api_key, model, timeout
        self.model = f"RapidOCR + {model}"

    def extract(self, pages: list[tuple[bytes, str]], hint: str | None) -> Extracted:
        started = time.monotonic()
        try:
            text = ocr_text(pages)
        except Exception as exc:  # the OCR library raises its own types
            raise ExtractionError(f"Local OCR failed: {exc}") from exc
        if not text.strip():
            raise ExtractionError("No text found on the page; check the photo or enter by hand")
        content = f"Document type hint from the user: {hint or 'unknown'}\n\nOCR text:\n{text}"
        data, usage = _chat_json(
            self.base_url, self.api_key, self.llm, self.timeout, OCR_PROMPT, content
        )
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
    engine = (s.vision_engine or "auto").lower()
    vision_ready = bool(s.vision_base_url and s.vision_model)
    ocr_ready = bool(s.llm_base_url and s.llm_model) and ocr_available()
    if engine in ("vision", "auto") and vision_ready:
        return OpenAICompatible(
            s.vision_base_url, s.vision_api_key, s.vision_model, s.vision_timeout_seconds
        )
    if engine in ("ocr", "auto") and ocr_ready:
        return OcrThenText(s.llm_base_url, s.llm_api_key, s.llm_model, s.vision_timeout_seconds)
    return None


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
