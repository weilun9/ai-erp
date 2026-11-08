"""Knowledge base: upload -> parse -> chunk -> embed -> search (design doc section 9).

Search is hybrid: keyword (Postgres full text) top 30 + vector (pgvector cosine) top 30,
merged with reciprocal rank fusion (RRF). Who may see a chunk is part of the same SQL
WHERE as the ranking, so hidden text is never fetched and then dropped in Python.
Without an embedder configured the search is keyword-only.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BusinessRule, NotFound
from app.models import KbChunk, KbDocument, Location, Tag
from app.services import audit
from app.services.auth import Principal
from app.services.embeddings import EmbedError, get_embedder
from app.services.kb_text import ParseError, chunk, heading, index_text, parse, query_text

CANDIDATES = 30
RRF_K = 60
# Plain RRF. Down-weighting keyword hits was tried with app.kb_eval (0.5, 0.3) and did
# not help, so both lists count the same.
WEIGHTS = {"k": 1.0, "v": 1.0}
CATEGORIES = ("SOP", "SUPPLIER", "TAX", "MANUAL", "PRODUCT", "OTHER")


# --- upload -------------------------------------------------------------------------------


def _check_visibility(db, p, tags, location_ids):
    if tags is not None:
        known = set(db.scalars(select(Tag.code).where(Tag.company_id == p.company_id)))
        bad = sorted(set(tags) - known)
        if bad:
            raise BusinessRule("Unknown tags", {"tags": bad})
    if location_ids is not None:
        known = set(db.scalars(select(Location.id).where(Location.company_id == p.company_id)))
        if set(location_ids) - known:
            raise BusinessRule("Unknown stores")


def upload(
    db: Session,
    p: Principal,
    *,
    content: bytes,
    filename: str,
    content_type: str,
    title: str,
    doc_key: str | None = None,
    category: str | None = None,
    visible_tags: list[str] | None = None,
    visible_location_ids: list[uuid.UUID] | None = None,
) -> tuple[KbDocument, list[str]]:
    """Add a document (or a new version of one with the same key). Returns warnings."""
    p.require("ai.kb.manage")
    s = get_settings()
    if len(content) > s.kb_max_mb * 1024 * 1024:
        raise BusinessRule(f"File is larger than {s.kb_max_mb} MB")
    if category is not None and category not in CATEGORIES:
        raise BusinessRule("Unknown category", {"allowed": list(CATEGORIES)})
    visible_tags = sorted(set(visible_tags)) if visible_tags else None
    visible_location_ids = list(visible_location_ids) if visible_location_ids else None
    _check_visibility(db, p, visible_tags, visible_location_ids)
    try:
        pieces = chunk(parse(content, filename))
    except ParseError as exc:
        raise BusinessRule(str(exc)) from exc

    warnings: list[str] = []
    texts = [heading(title, c.section) + "\n" + c.content for c in pieces]
    vectors: list[list[float]] | None = None
    embed_name = None
    embedder = get_embedder()
    if embedder is not None:
        try:
            vectors, embed_name = embedder.documents(texts), embedder.name
        except EmbedError as exc:
            warnings.append(f"Embedding failed, keyword search only: {exc}")

    key = (doc_key or title).strip().lower()[:80]
    current = db.scalar(
        select(KbDocument)
        .where(
            KbDocument.company_id == p.company_id,
            KbDocument.doc_key == key,
            KbDocument.status == "READY",
        )
        .with_for_update()
    )
    version = 1
    if current is not None:
        _retire(db, current)
        version = current.version + 1
        warnings.append(f"Version {current.version} retired; this is version {version}")

    digest = hashlib.sha256(content).hexdigest()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    storage_key = f"{p.company_id}/{digest}.{ext}"
    path = s.kb_path / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)

    doc = KbDocument(
        company_id=p.company_id,
        doc_key=key,
        version=version,
        title=title,
        category=category,
        filename=filename[:255],
        content_type=content_type[:80],
        sha256=digest,
        storage_key=storage_key,
        visible_tags=visible_tags,
        visible_location_ids=visible_location_ids,
        chunks=len(pieces),
        embed_model=embed_name,
        error=warnings[0][:500] if vectors is None and embedder is not None else None,
        created_by=p.user_id,
    )
    db.add(doc)
    db.flush()
    for i, (c, text) in enumerate(zip(pieces, texts, strict=True)):
        db.add(
            KbChunk(
                company_id=p.company_id,
                document_id=doc.id,
                seq=c.seq,
                section=(c.section or None) and c.section[:300],
                page=c.page,
                content=c.content,
                tsv=func.to_tsvector("simple", index_text(text)),
                embedding=vectors[i] if vectors else None,
                embed_model=embed_name,
                visible_tags=visible_tags,
                visible_location_ids=visible_location_ids,
            )
        )
    audit.record(
        db,
        p,
        "kb_upload",
        "ai.kb_document",
        doc.id,
        after={"title": title, "version": version, "chunks": len(pieces)},
    )
    db.commit()
    return doc, warnings


def _retire(db: Session, doc: KbDocument) -> None:
    doc.status, doc.retired_at = "RETIRED", datetime.now(UTC)
    db.execute(update(KbChunk).where(KbChunk.document_id == doc.id).values(is_active=False))
    db.flush()


def retire(db: Session, p: Principal, document_id: uuid.UUID) -> KbDocument:
    p.require("ai.kb.manage")
    doc = get(db, p, document_id)
    if doc.status == "READY":
        _retire(db, doc)
        audit.record(db, p, "kb_retire", "ai.kb_document", doc.id)
        db.commit()
    return doc


def get(db: Session, p: Principal, document_id: uuid.UUID) -> KbDocument:
    p.require("ai.kb.manage")
    doc = db.get(KbDocument, document_id)
    if doc is None or doc.company_id != p.company_id:
        raise NotFound("Document not found")
    return doc


def documents(db: Session, p: Principal, *, include_retired: bool = False) -> list[KbDocument]:
    p.require("ai.kb.manage")
    stmt = select(KbDocument).where(KbDocument.company_id == p.company_id)
    if not include_retired:
        stmt = stmt.where(KbDocument.status == "READY")
    return list(db.scalars(stmt.order_by(KbDocument.title, KbDocument.version.desc())))


def chunks(db: Session, p: Principal, document_id: uuid.UUID) -> list[KbChunk]:
    doc = get(db, p, document_id)
    return list(
        db.scalars(select(KbChunk).where(KbChunk.document_id == doc.id).order_by(KbChunk.seq))
    )


# --- search -------------------------------------------------------------------------------


@dataclass
class Hit:
    chunk_id: int
    document_id: uuid.UUID
    title: str
    version: int
    category: str | None
    section: str | None
    page: int | None
    content: str
    score: float
    keyword_rank: int | None
    vector_rank: int | None


def _visible(p: Principal):
    """The visibility filter, as SQL (applied before ranking)."""
    conds = [KbChunk.company_id == p.company_id, KbChunk.is_active.is_(True)]
    if not p.has("ai.kb.manage"):  # managers see everything to check what they uploaded
        conds.append(
            or_(KbChunk.visible_tags.is_(None), KbChunk.visible_tags.overlap(sorted(p.tags)))
        )
    if p.location_ids is not None:
        conds.append(
            or_(
                KbChunk.visible_location_ids.is_(None),
                KbChunk.visible_location_ids.overlap(list(p.location_ids)),
            )
        )
    return conds


def search(
    db: Session, p: Principal, query: str, *, limit: int = 6, mode: str | None = None
) -> list[Hit]:
    """mode: keyword | vector | hybrid; default from KB_SEARCH_MODE ("auto": vector when an
    embedding model is set, else keyword). Measured with app.kb_eval on the sample set,
    vector alone beat hybrid (95% vs 90% hit@6): the loose Chinese two-character keyword
    terms pull in the wrong document across languages. Rerun it as the library grows."""
    p.require("ai.kb.read")
    if mode is None:
        mode = get_settings().kb_search_mode
        if mode == "auto":
            mode = "vector" if get_embedder() is not None else "keyword"
    query = query.strip()[:500]
    if not query:
        return []
    ranks: dict[int, dict[str, int]] = {}

    q = query_text(query)
    if q and mode in ("hybrid", "keyword"):
        tsq = func.to_tsquery("simple", q)
        rows = db.execute(
            select(KbChunk.id)
            .where(*_visible(p), KbChunk.tsv.op("@@")(tsq))
            .order_by(func.ts_rank_cd(KbChunk.tsv, tsq).desc(), KbChunk.id)
            .limit(CANDIDATES)
        ).scalars()
        for i, cid in enumerate(rows, 1):
            ranks.setdefault(cid, {})["k"] = i

    embedder = get_embedder() if mode in ("hybrid", "vector") else None
    if embedder is not None:
        try:
            qv = embedder.query(query)
        except EmbedError:
            qv = None
        if qv is not None:
            rows = db.execute(
                select(KbChunk.id)
                .where(*_visible(p), KbChunk.embed_model == embedder.name)
                .order_by(KbChunk.embedding.cosine_distance(qv), KbChunk.id)
                .limit(CANDIDATES)
            ).scalars()
            for i, cid in enumerate(rows, 1):
                ranks.setdefault(cid, {})["v"] = i

    scored = sorted(
        ((sum(WEIGHTS[m] / (RRF_K + r) for m, r in rk.items()), cid) for cid, rk in ranks.items()),
        key=lambda x: (-x[0], x[1]),
    )[:limit]
    if not scored:
        return []
    ids = [cid for _, cid in scored]
    rows = {
        c.id: (c, d)
        for c, d in db.execute(
            select(KbChunk, KbDocument)
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(KbChunk.id.in_(ids), *_visible(p))
        )
    }
    return [
        Hit(
            chunk_id=cid,
            document_id=rows[cid][1].id,
            title=rows[cid][1].title,
            version=rows[cid][1].version,
            category=rows[cid][1].category,
            section=rows[cid][0].section,
            page=rows[cid][0].page,
            content=rows[cid][0].content,
            score=round(score, 5),
            keyword_rank=ranks[cid].get("k"),
            vector_rank=ranks[cid].get("v"),
        )
        for score, cid in scored
        if cid in rows
    ]


def cited(hits: list[Hit]) -> dict[str, Any]:
    """What the AI gets: numbered sources it must cite as [1], [2]..."""
    if not hits:
        return {
            "sources": [],
            "note": "Nothing in the knowledge base matches. Say the knowledge base has no "
            "information on this; do not answer from general knowledge.",
        }
    return {
        "sources": [
            {
                "ref": f"[{i}]",
                "document": f"{h.title} (v{h.version})",
                "section": h.section,
                "page": h.page,
                "text": h.content,
            }
            for i, h in enumerate(hits, 1)
        ],
        "note": "Answer only from these sources and cite them like [1]. If they do not answer "
        "the question, say so.",
    }
