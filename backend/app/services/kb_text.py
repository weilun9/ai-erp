"""Turning a document into searchable chunks, and text into keyword-search terms.

Parse: text / Markdown (# headings), PDF (text per page, with page numbers), Word .docx
(Heading styles). Scanned PDFs without a text layer are refused for now (no OCR yet).

Chunk: by section, then paragraphs, about TARGET characters, with ~15% overlap between
neighbouring chunks of the same section. The title and section path are put in front of
every chunk when it is embedded and indexed, which helps short chunks get found.

Keyword terms: Postgres' 'simple' text search splits on spaces, which is fine for English
and Malay but sees a Chinese sentence as one word. Chinese runs are therefore turned into
overlapping two-character pieces (退货流程 -> 退货 货流 流程) for both chunks and queries.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

TARGET = 700
MAX_PARA = 900
OVERLAP = 0.15

STOP = {
    # English
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "if", "in", "is", "it", "of", "on", "or", "our", "the", "to", "we", "what",
    "when", "where", "which", "who", "with", "you", "your",
    # Malay
    "ada", "adalah", "apa", "bagaimana", "dan", "dari", "di", "ini", "itu", "ke", "kita",
    "saya", "untuk", "yang", "dengan", "boleh", "kami",
}  # fmt: skip

CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
WORD = re.compile(r"[0-9a-zÀ-ɏ]+(?:[.\-'][0-9a-zÀ-ɏ]+)*")
SENTENCE_END = re.compile(r"(?<=[。！？!?.；;])\s*")


@dataclass
class Block:
    section: str | None
    page: int | None
    text: str


@dataclass
class Chunk:
    seq: int
    section: str | None
    page: int | None
    content: str


class ParseError(Exception):
    pass


# --- parsing ------------------------------------------------------------------------------


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _markdown(text: str) -> list[Block]:
    blocks: list[Block] = []
    path: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush():
        section = " > ".join(t for _, t in path) or None
        for para in _paragraphs("\n".join(buf)):
            blocks.append(Block(section, None, para))
        buf.clear()

    lines = text.splitlines()
    tops = [ln for ln in lines if re.match(r"^#\s+", ln.strip())]
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    # A single "# Heading" at the very top is the document's title, not a section.
    title_line = first if len(tops) == 1 and first.startswith("# ") else None

    for line in lines:
        if title_line is not None and line.strip() == title_line:
            title_line = None
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            flush()
            level = len(m.group(1))
            path[:] = [(lvl, t) for lvl, t in path if lvl < level] + [(level, m.group(2).strip())]
        else:
            buf.append(line)
    flush()
    return blocks


def _pdf(content: bytes) -> list[Block]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(content)
    blocks = []
    for i in range(len(pdf)):
        text = pdf[i].get_textpage().get_text_range()
        for para in _paragraphs(text.replace("\r\n", "\n")):
            blocks.append(Block(None, i + 1, para))
    if not blocks:
        raise ParseError("This PDF has no text layer (a scan?); OCR is not supported yet")
    return blocks


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(content: bytes) -> list[Block]:
    try:
        xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ParseError("Not a readable .docx file") from exc
    path: list[tuple[int, str]] = []
    blocks = []
    for p in ElementTree.fromstring(xml).iter(f"{W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{W}t")).strip()
        if not text:
            continue
        style = p.find(f"{W}pPr/{W}pStyle")
        name = (style.get(f"{W}val") or "") if style is not None else ""
        m = re.match(r"(?i)^(heading|title)(\d?)$", name)
        if m:
            if m.group(1).lower() == "title":  # the document's title, not a section
                continue
            level = int(m.group(2) or 1)
            path[:] = [(lvl, t) for lvl, t in path if lvl < level] + [(level, text)]
        else:
            blocks.append(Block(" > ".join(t for _, t in path) or None, None, text))
    return blocks


def parse(content: bytes, filename: str) -> list[Block]:
    name = filename.lower()
    if name.endswith(".pdf"):
        blocks = _pdf(content)
    elif name.endswith(".docx"):
        blocks = _docx(content)
    elif name.endswith((".txt", ".md", ".markdown")):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("Text files must be UTF-8") from exc
        blocks = _markdown(text)
    else:
        raise ParseError("Supported: .pdf, .docx, .txt, .md")
    if not blocks:
        raise ParseError("No text found in this file")
    return blocks


# --- chunking -----------------------------------------------------------------------------


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_PARA:
        return [text]
    parts, cur = [], ""
    for sentence in SENTENCE_END.split(text):
        if cur and len(cur) + len(sentence) > TARGET:
            parts.append(cur)
            cur = ""
        cur = (cur + " " + sentence).strip() if cur and not CJK.match(sentence) else cur + sentence
    if cur:
        parts.append(cur)
    out = []
    for part in parts:  # a "sentence" with no end mark at all
        out += [part[i : i + MAX_PARA] for i in range(0, len(part), MAX_PARA)]
    return out


def _tail(text: str) -> str:
    """The last ~15% of a chunk, cut at a sentence start where possible."""
    want = int(len(text) * OVERLAP)
    tail = text[-want:] if want else ""
    m = SENTENCE_END.search(tail)
    return tail[m.end() :] if m and m.end() < len(tail) else tail


def chunk(blocks: list[Block]) -> list[Chunk]:
    out: list[Chunk] = []
    cur: list[str] = []
    key: tuple[str | None, int | None] | None = None

    def flush(carry: bool):
        nonlocal cur
        if cur:
            text = "\n".join(cur)
            out.append(Chunk(len(out), key[0], key[1], text))
            cur = [_tail(text)] if carry and _tail(text) else []

    for b in blocks:
        k = (b.section, b.page)
        if k != key:
            flush(carry=False)
            key = k
        for piece in _split_long(b.text):
            if cur and sum(len(c) for c in cur) + len(piece) > TARGET:
                flush(carry=True)
            cur.append(piece)
    flush(carry=False)
    return out


def heading(title: str, section: str | None) -> str:
    return f"{title} > {section}" if section else title


# --- keyword terms ------------------------------------------------------------------------


def terms(text: str) -> list[str]:
    text = text.lower()
    out: list[str] = []
    for run in CJK.findall(text):
        out += [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
    for w in WORD.findall(CJK.sub(" ", text)):
        if w not in STOP:
            out.append(w)
    return out


def index_text(text: str) -> str:
    """What goes into to_tsvector('simple', ...)."""
    return " ".join(terms(text))


def query_text(text: str) -> str | None:
    """An OR query for to_tsquery('simple', ...); None if there is nothing to look for."""
    seen = list(dict.fromkeys(terms(text)))
    return " | ".join("'" + t.replace("'", "''") + "'" for t in seen) or None
