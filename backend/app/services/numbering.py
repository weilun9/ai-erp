import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import DocSequence

DEFAULT_PREFIX = {"GRN": "GRN", "SCN": "SCN"}


def next_doc_no(db: Session, company_id: uuid.UUID, doc_type: str) -> str:
    """Take the next number for this document type.

    The sequence row stays locked until the caller's transaction ends, so concurrent saves
    never get the same number, and a rolled-back save gives its number back.
    """
    db.execute(
        insert(DocSequence)
        .values(company_id=company_id, doc_type=doc_type, prefix=DEFAULT_PREFIX[doc_type])
        .on_conflict_do_nothing()
    )
    seq = db.scalar(
        select(DocSequence)
        .where(DocSequence.company_id == company_id, DocSequence.doc_type == doc_type)
        .with_for_update()
    )
    no = f"{seq.prefix}{seq.next_no:0{seq.width}d}"
    seq.next_no += 1
    return no
