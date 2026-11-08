import hashlib
import io
import json
import math
import zipfile

import pytest

from app.kb_samples import upload_all
from app.services import embeddings
from app.services.embeddings import EmbedError
from app.services.kb_text import chunk, parse, query_text, terms
from tests.conftest import login
from tests.test_ai import Scripted
from tests.test_sales import store  # noqa: F401

KB = "/api/v1/kb"


class Hashing:
    """A stand-in embedding model: hashed bag of search terms (no download in CI)."""

    name = "test-hashing"

    def _vec(self, text):
        v = [0.0] * 64
        for t in terms(text):
            v[int(hashlib.md5(t.encode()).hexdigest(), 16) % 64] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        return [x / n for x in v]

    def documents(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


class Broken(Hashing):
    def documents(self, texts):
        raise EmbedError("endpoint down")


@pytest.fixture
def embedder():
    embeddings.set_embedder(Hashing())
    yield
    embeddings.reset_embedder()


@pytest.fixture
def library(store, client, embedder):  # noqa: F811
    def post(path, files, data):
        r = client.post("/api/v1" + path, files=files, data=data, headers=store["h"])
        assert r.status_code == 201, r.text
        return r.json()

    return {**store, "ids": upload_all(post)}


def _search(client, h, q):
    r = client.get(f"{KB}/search", params={"q": q}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# --- text handling (no database) ----------------------------------------------------------


def test_chinese_is_split_into_two_character_terms() -> None:
    assert terms("退货流程 for Nestle, dan RM50") == ["退货", "货流", "流程", "nestle", "rm50"]
    assert query_text("the and") is None
    assert query_text("O'Brien 退货") == "'退货' | 'o''brien'"


def test_markdown_sections_and_overlap() -> None:
    long = "。".join(f"第{i}句话讲退货的规定和细节" for i in range(80)) + "。"
    text = f"# Title\n\n## Returns\n### Receipt\n{long}\n\n## Shift\nCount the cash."
    pieces = chunk(parse(text.encode(), "rules.txt"))
    sections = [c.section for c in pieces]
    assert sections[0] == "Returns > Receipt" and sections[-1] == "Shift"  # H1 = title
    first, second = pieces[0].content, pieces[1].content
    assert len(first) <= 900
    carried = second.splitlines()[0]  # ~15% of the first chunk starts the second
    assert carried and carried in first and len(carried) < len(first) / 4


def test_docx_headings_become_sections() -> None:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def para(text, style=None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"

    xml = f"<w:document {w}><w:body>{para('Returns', 'Heading1')}{para('Within 7 days.')}"
    xml += f"{para('Cash', 'Heading2')}{para('Count it.')}</w:body></w:document>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    blocks = parse(buf.getvalue(), "sop.docx")
    assert [(b.section, b.text) for b in blocks] == [
        ("Returns", "Within 7 days."),
        ("Returns > Cash", "Count it."),
    ]


# --- library ------------------------------------------------------------------------------


def test_upload_chunks_and_versions(library, client) -> None:
    h = library["h"]
    docs = {d["doc_key"]: d for d in client.get(f"{KB}/documents", headers=h).json()}
    assert set(docs) == {"sop", "supplier", "sst", "manual"}
    assert docs["supplier"]["visible_tags"] == ["accountant", "clerk", "owner"]
    assert docs["sop"]["embed_model"] == "test-hashing" and docs["sop"]["chunks"] >= 5

    chunks = client.get(f"{KB}/documents/{docs['sop']['id']}/chunks", headers=h).json()
    assert chunks[0]["section"] is None  # the intro under the title
    assert chunks[1]["section"] == "顾客退货与换货 > 可以退货的条件"
    assert all(c["has_vector"] for c in chunks)

    files = {"file": ("sop.txt", b"# Rules\n\nReturns within 3 days now.", "text/plain")}
    r = client.post(
        f"{KB}/documents", files=files, data={"title": "SOP", "doc_key": "sop"}, headers=h
    ).json()
    assert r["version"] == 2 and "Version 1 retired" in r["warnings"][0]
    hits = _search(client, h, "顾客没有收据可以退货吗")
    assert {x["version"] for x in hits if x["title"] == "SOP"} <= {2}
    assert not [x for x in hits if x["title"].startswith("门市")]  # old version is gone


def test_search_finds_across_languages_with_citations(library, client) -> None:
    hits = _search(client, library["h"], "退款超过 RM50 要谁批准？")
    assert hits[0]["title"].startswith("门市作业规定")
    assert any("退款方式" in (x["section"] or "") for x in hits[:3])


def test_visibility_is_filtered_in_the_search(library, client) -> None:  # noqa: F811
    cashier = login(client, "cashier1")
    hits = _search(client, cashier, "Nestle minimum order rebate credit note")
    assert hits and {x["title"] for x in hits} <= {
        "门市作业规定：退货与收银班结",
        "Panduan sistem untuk kakitangan",
    }
    assert client.get(f"{KB}/documents", headers=cashier).status_code == 403


def test_store_scope(library, client) -> None:  # noqa: F811
    files = {"file": ("s2.txt", b"# S2 only\n\nS2 opens at 9am on Sundays.", "text/plain")}
    data = {"title": "S2 notice", "visible_location_ids": library["hq"]}  # HQ only
    r = client.post(f"{KB}/documents", files=files, data=data, headers=library["h"])
    assert r.status_code == 201, r.text
    cashier = login(client, "cashier1")  # works at S2
    assert not [x for x in _search(client, cashier, "opens Sundays") if x["title"] == "S2 notice"]


def test_bad_files_and_permissions(library, client) -> None:  # noqa: F811
    h = library["h"]
    bad = client.post(
        f"{KB}/documents",
        files={"file": ("x.xlsx", b"PK..", "application/octet-stream")},
        data={"title": "x"},
        headers=h,
    )
    assert bad.status_code == 422
    unknown = client.post(
        f"{KB}/documents",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"title": "a", "visible_tags": "nobody"},
        headers=h,
    )
    assert unknown.status_code == 422 and unknown.json()["error"]["details"]["tags"] == ["nobody"]
    clerk = login(client, "clerk1")
    r = client.post(
        f"{KB}/documents",
        files={"file": ("a.txt", b"hi", "text/plain")},
        data={"title": "a"},
        headers=clerk,
    )
    assert r.status_code == 403


def test_embedding_failure_falls_back_to_keywords(store, client) -> None:  # noqa: F811
    embeddings.set_embedder(Broken())
    try:
        files = {"file": ("a.txt", b"# Lunch\n\nLunch break is 12 to 1pm.", "text/plain")}
        r = client.post(
            f"{KB}/documents", files=files, data={"title": "Hours"}, headers=store["h"]
        ).json()
        assert r["embed_model"] is None and "keyword search only" in r["warnings"][0]
        embeddings.set_embedder(None)
        assert _search(client, store["h"], "lunch break")[0]["title"] == "Hours"
    finally:
        embeddings.reset_embedder()


# --- the butler uses it -------------------------------------------------------------------


@pytest.fixture
def chat_model():
    from app.services import llm

    def use(*steps):
        m = Scripted(*steps)
        llm.set_chat_model(m)
        return m

    yield use
    from app.services import llm as _llm

    _llm.reset_chat_model()


def _ask(client, h, text):
    conv = client.post("/api/v1/ai/conversations", json={}, headers=h).json()["id"]
    r = client.post(f"/api/v1/ai/conversations/{conv}/messages", json={"content": text}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_butler_gets_numbered_sources(library, client, chat_model) -> None:
    m = chat_model(("tool", "search_knowledge", {"query": "service tax SV8"}), ("say", "[1]"))
    _ask(client, library["h"], "what is SV8?")
    res = m.results()[0]["result"]
    assert res["sources"][0]["ref"] == "[1]"
    assert res["sources"][0]["document"] == "SST and e-invoice notes (v1)"
    assert "cite them" in res["note"]


def test_butler_is_told_when_nothing_matches(store, client, chat_model) -> None:  # noqa: F811
    m = chat_model(("tool", "search_knowledge", {"query": "parking rules"}), ("say", "none"))
    _ask(client, store["h"], "parking?")
    res = m.results()[0]["result"]
    assert res["sources"] == [] and "no information" in res["note"]
    assert json.dumps(res)  # plain data for the model
