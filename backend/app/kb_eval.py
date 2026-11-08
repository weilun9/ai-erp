"""Retrieval evaluation for the knowledge base.

    python -m app.kb_eval

Builds its own database (erp_kbeval), uploads the sample documents in backend/demo_kb,
then asks every question in demo_kb/eval.json and checks whether the right document
section comes back. Reports, for keyword, vector and hybrid search:
  hit@1  the right section is the first result
  hit@6  it is in the 6 the AI gets
  MRR    mean of 1/rank (1.0 = always first)
Rerun after changing chunking, the embedding model or the ranking.

Results on the 21 sample questions (2026-11-08), hit@6:
  keyword only                                71%
  paraphrase-multilingual-MiniLM-L12-v2       vector 95%, hybrid (RRF) 90%
  multilingual-e5-large                       vector 78%, hybrid 83%
So the default is the small MiniLM model with vector search (KB_SEARCH_MODE=auto).
"""

import os
import sys
import uuid
import warnings

DB_NAME = "erp_kbeval"
os.environ["POSTGRES_DB"] = DB_NAME
os.environ.setdefault("APP_ENV", "dev")
warnings.filterwarnings("ignore", category=DeprecationWarning)

import psycopg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import decode_access_token  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.kb_samples import eval_set, upload_all  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Device  # noqa: E402
from app.services import kb  # noqa: E402
from app.services.auth import load_principal  # noqa: E402
from app.services.embeddings import get_embedder  # noqa: E402
from app.services.seed import bootstrap  # noqa: E402

PASSWORD = "eval-password-2026"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _fresh() -> None:
    s = get_settings()
    with psycopg.connect(
        host=s.postgres_host,
        port=s.postgres_port,
        user=s.postgres_user,
        password=s.postgres_password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    cfg = Config()
    cfg.set_main_option("script_location", str(get_settings().kb_path.parents[1] / "alembic"))
    command.upgrade(cfg, "head")
    with SessionLocal() as db:
        bootstrap(
            db,
            company_code="EVAL",
            company_name="Eval Shop",
            hq_code="HQ",
            hq_name="HQ",
            admin_username="owner",
            admin_password=PASSWORD,
        )
        db.commit()


def _login(client: TestClient, username: str) -> dict[str, str]:
    body = {"username": username, "password": PASSWORD, "remember": True}
    r = client.post("/api/v1/auth/login", json=body)
    if r.status_code == 403:
        with SessionLocal() as db:
            device_id = uuid.UUID(r.json()["error"]["details"]["device_id"])
            db.get(Device, device_id).is_authorized = True
            db.commit()
        r = client.post("/api/v1/auth/login", json=body)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _principal(db, headers):
    claims = decode_access_token(headers["Authorization"].split()[1])
    return load_principal(db, claims.user_id, claims.session_id)


def main() -> None:
    _fresh()
    client = TestClient(app)
    h = _login(client, "owner")

    def post(path, files, data):
        r = client.post("/api/v1" + path, files=files, data=data, headers=h)
        assert r.status_code == 201, r.text
        return r.json()

    ids = upload_all(post)
    key_of = {v: k for k, v in ids.items()}
    embedder = get_embedder()
    print(f"Embedding model: {embedder.name if embedder else 'none (keyword only)'}")
    questions = eval_set()
    modes = ["keyword", "vector", "hybrid"] if embedder else ["keyword"]

    with SessionLocal() as db:
        p = _principal(db, h)
        misses: dict[str, list[str]] = {m: [] for m in modes}
        print(f"\n{'mode':<8} {'hit@1':>6} {'hit@6':>6} {'MRR':>6}   ({len(questions)} questions)")
        for mode in modes:
            first = top6 = 0
            rr = 0.0
            for item in questions:
                hits = kb.search(db, p, item["q"], limit=6, mode=mode)
                rank = next(
                    (
                        i
                        for i, x in enumerate(hits, 1)
                        if key_of[str(x.document_id)] == item["doc"]
                        and item["section"].lower() in (x.section or "").lower()
                    ),
                    None,
                )
                if rank == 1:
                    first += 1
                if rank:
                    top6 += 1
                    rr += 1 / rank
                else:
                    misses[mode].append(item["q"])
            n = len(questions)
            print(f"{mode:<8} {first / n:>6.0%} {top6 / n:>6.0%} {rr / n:>6.2f}")
        for mode, qs in misses.items():
            for q in qs:
                print(f"  missed by {mode}: {q}")

    # Visibility: a cashier must not get supplier terms or tax notes back at all.
    with SessionLocal() as db:
        owner = _principal(db, h)
        cashier_tag = next(t for t in owner_tags(db, owner) if t[1] == "cashier")[0]
    client.post(
        "/api/v1/users",
        json={
            "username": "kasir",
            "display_name": "Cashier",
            "password": PASSWORD,
            "tag_ids": [str(cashier_tag)],
        },
        headers=h,
    )
    counter = TestClient(app)
    ch = _login(counter, "kasir")
    with SessionLocal() as db:
        cp = _principal(db, ch)
        seen = {
            key_of[str(x.document_id)]
            for item in questions
            for x in kb.search(db, cp, item["q"], limit=6)
        }
    print(f"\nCashier can retrieve from: {sorted(seen)} (supplier and sst must be absent)")
    if seen & {"supplier", "sst"}:
        sys.exit("FAIL: a cashier retrieved documents hidden from cashiers")


def owner_tags(db, p):
    from sqlalchemy import select

    from app.models import Tag

    return db.execute(select(Tag.id, Tag.code).where(Tag.company_id == p.company_id)).all()


if __name__ == "__main__":
    main()
