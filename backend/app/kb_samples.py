"""The sample knowledge base in backend/demo_kb, used by the demo and the evaluation."""

import json
from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "demo_kb"

# key, file, title, category, visible tags (None = everyone)
DOCS = [
    ("sop", "sop_returns_and_shift.txt", "门市作业规定：退货与收银班结", "SOP", None),
    (
        "supplier",
        "supplier_terms.txt",
        "Supplier trading terms",
        "SUPPLIER",
        ["owner", "accountant", "clerk"],
    ),
    ("sst", "sst_einvoice_notes.txt", "SST and e-invoice notes", "TAX", ["owner", "accountant"]),
    ("manual", "panduan_sistem.txt", "Panduan sistem untuk kakitangan", "MANUAL", None),
]


def upload_all(post) -> dict[str, str]:
    """post(path, files, data) -> response JSON. Returns doc key -> document id."""
    ids = {}
    for key, name, title, category, tags in DOCS:
        data = {"title": title, "doc_key": key, "category": category}
        if tags:
            data["visible_tags"] = ",".join(tags)
        files = {"file": (name, (DIR / name).read_bytes(), "text/plain")}
        ids[key] = post("/kb/documents", files, data)["id"]
    return ids


def eval_set() -> list[dict[str, str]]:
    return json.loads((DIR / "eval.json").read_text(encoding="utf-8"))
