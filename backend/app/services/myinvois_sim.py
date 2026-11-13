"""A stand-in for LHDN MyInvois, used only on the public demo site (DEMO_MODE with no
MyInvois credentials). It answers in the same shapes as the real API so the whole
e-invoice flow - check, submit, validate, QR on the printout, cancel - can be tried
without sending anything to LHDN. Everything it issues is marked SIM and is not a valid
e-invoice.

It keeps no state (the demo runs as short-lived serverless functions): a document it
accepted is always reported Valid, and its long id is derived from the UUID.
"""

import base64
import hashlib
import secrets
import string
from datetime import UTC, datetime
from typing import Any

from app.services.myinvois import Encoded, MyInvoisError

ENVIRONMENT = "simulated"
_ALPHABET = string.ascii_uppercase + string.digits


def _long_id(uuid: str) -> str:
    return hashlib.sha256(f"sim:{uuid}".encode()).hexdigest()[:40].upper()


class SimulatedMyInvois:
    environment = ENVIRONMENT

    def submit(self, docs: list[tuple[str, Encoded]]) -> dict[str, Any]:
        accepted, rejected = [], []
        for no, enc in docs:
            raw = base64.b64decode(enc.document)
            if hashlib.sha256(raw).hexdigest() != enc.document_hash:  # what LHDN checks first
                rejected.append(
                    {
                        "invoiceCodeNumber": no,
                        "error": {"code": "BadArgument", "message": "Document hash does not match"},
                    }
                )
                continue
            uuid = "SIM" + "".join(secrets.choice(_ALPHABET) for _ in range(23))
            accepted.append({"uuid": uuid, "invoiceCodeNumber": no})
        return {
            "submissionUID": "SIM" + "".join(secrets.choice(_ALPHABET) for _ in range(12)),
            "acceptedDocuments": accepted,
            "rejectedDocuments": rejected,
        }

    def details(self, uuid: str) -> dict[str, Any]:
        if not uuid.startswith("SIM"):
            raise MyInvoisError("Cannot read the document from MyInvois", 404, None)
        return {
            "uuid": uuid,
            "status": "Valid",
            "longId": _long_id(uuid),
            "dateTimeValidated": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

    def cancel(self, uuid: str, reason: str) -> dict[str, Any]:
        return {"uuid": uuid, "status": "Cancelled"}
