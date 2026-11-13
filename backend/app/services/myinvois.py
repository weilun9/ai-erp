"""LHDN MyInvois API client (endpoints per the MyInvois SDK, checked 2026-11-08).

- Login: POST {identity}/connect/token, client_credentials, scope InvoicingAPI; the token
  lives 60 minutes and is reused (login is limited to 12 requests a minute). An
  intermediary adds the header `onbehalfof: <taxpayer TIN>`.
- Submit: POST /api/v1.0/documentsubmissions/ -> 202 with submissionUID,
  acceptedDocuments[{uuid, invoiceCodeNumber}], rejectedDocuments[{invoiceCodeNumber,
  error}]. Up to 100 documents / 5 MB per submission, 300 KB per document.
- Document details: GET /api/v1.0/documents/{uuid}/details (status, longId,
  validationResults).
- Cancel: PUT /api/v1.0/documents/state/{uuid}/state {status: cancelled, reason}, allowed
  within 72 hours of validation.
"""

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings

ENVIRONMENTS = {
    "sandbox": {
        "api": "https://preprod-api.myinvois.hasil.gov.my",
        "portal": "https://preprod.myinvois.hasil.gov.my",
    },
    "production": {
        "api": "https://api.myinvois.hasil.gov.my",
        "portal": "https://myinvois.hasil.gov.my",
    },
}


class MyInvoisError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status, self.body = status, body


@dataclass
class Encoded:
    document: str  # base64 of the exact bytes hashed
    document_hash: str  # SHA-256 (hex) of those bytes
    raw: bytes


def encode(document: dict[str, Any]) -> Encoded:
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Encoded(base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest(), raw)


def share_url(environment: str, uuid: str, long_id: str) -> str:
    """Public validation link LHDN prints as the QR code on the invoice."""
    if environment not in ENVIRONMENTS:  # the demo site's stand-in: say so in the QR itself
        return f"SIMULATED e-invoice (public demo, not valid with LHDN) {uuid} / {long_id}"
    return f"{ENVIRONMENTS[environment]['portal']}/{uuid}/share/{long_id}"


class MyInvoisClient:
    def __init__(
        self,
        environment: str,
        client_id: str,
        client_secret: str,
        *,
        onbehalfof: str = "",
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if environment not in ENVIRONMENTS:
            raise ValueError(f"MYINVOIS_ENV must be one of {sorted(ENVIRONMENTS)}")
        self.environment = environment
        self.client_id, self.client_secret, self.onbehalfof = client_id, client_secret, onbehalfof
        self.http = httpx.Client(
            base_url=ENVIRONMENTS[environment]["api"], timeout=timeout, transport=transport
        )
        self._token: str | None = None
        self._token_until = 0.0

    # --- auth ---------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token_until:
            self._login()
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _login(self) -> None:
        headers = {"onbehalfof": self.onbehalfof} if self.onbehalfof else {}
        try:
            r = self.http.post(
                "/connect/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                    "scope": "InvoicingAPI",
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise MyInvoisError(f"Cannot reach MyInvois: {exc}") from exc
        if r.status_code != 200:
            raise MyInvoisError("MyInvois login failed", r.status_code, _body(r))
        body = r.json()
        self._token = body["access_token"]
        # renew a minute early
        self._token_until = time.monotonic() + max(int(body.get("expires_in", 3600)) - 60, 60)

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in (1, 2):
            try:
                r = self.http.request(method, path, headers=self._headers(), **kw)
            except httpx.HTTPError as exc:
                raise MyInvoisError(f"Cannot reach MyInvois: {exc}") from exc
            if r.status_code == 401 and attempt == 1:  # token expired early: log in again
                self._token = None
                continue
            return r
        return r

    # --- documents ------------------------------------------------------------------------

    def submit(self, docs: list[tuple[str, Encoded]]) -> dict[str, Any]:
        """docs = [(our document number, encoded document)]"""
        body = {
            "documents": [
                {
                    "format": "JSON",
                    "document": enc.document,
                    "documentHash": enc.document_hash,
                    "codeNumber": no,
                }
                for no, enc in docs
            ]
        }
        r = self._call("POST", "/api/v1.0/documentsubmissions/", json=body)
        if r.status_code != 202:
            raise MyInvoisError("MyInvois refused the submission", r.status_code, _body(r))
        return r.json()

    def details(self, uuid: str) -> dict[str, Any]:
        r = self._call("GET", f"/api/v1.0/documents/{uuid}/details")
        if r.status_code != 200:
            raise MyInvoisError("Cannot read the document from MyInvois", r.status_code, _body(r))
        return r.json()

    def cancel(self, uuid: str, reason: str) -> dict[str, Any]:
        r = self._call(
            "PUT",
            f"/api/v1.0/documents/state/{uuid}/state",
            json={"status": "cancelled", "reason": reason[:300]},
        )
        if r.status_code != 200:
            raise MyInvoisError("MyInvois did not cancel the document", r.status_code, _body(r))
        return r.json()


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:500]


_override: MyInvoisClient | None = None
_overridden = False
_cached: MyInvoisClient | None = None


def set_client(client: MyInvoisClient | None) -> None:
    global _override, _overridden
    _override, _overridden = client, True


def reset_client() -> None:
    global _override, _overridden, _cached
    _override, _overridden, _cached = None, False, None


def get_client() -> MyInvoisClient | None:
    """The configured client (kept, so its token is reused), or None when not set up."""
    global _cached
    if _overridden:
        return _override
    s = get_settings()
    if not (s.myinvois_client_id and s.myinvois_client_secret):
        if s.demo_mode:  # public demo: a stand-in that never talks to LHDN
            from app.services.myinvois_sim import SimulatedMyInvois

            return SimulatedMyInvois()  # type: ignore[return-value]
        return None
    if _cached is None:
        _cached = MyInvoisClient(
            s.myinvois_env,
            s.myinvois_client_id,
            s.myinvois_client_secret,
            onbehalfof=s.myinvois_onbehalfof,
            timeout=s.myinvois_timeout_seconds,
        )
    return _cached
