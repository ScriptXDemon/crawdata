"""Client that POSTs one raw page bundle per kept document to the Ingest API.

Two transports:
  * ``HttpIngestClient`` — real ``POST /ingest/v1/page`` over HTTP (production).
  * ``InProcessIngestClient`` — calls the FastAPI app via Starlette's TestClient,
    so the test harness proves the full POST + acceptance path without
    binding a port.

Both send the bundle ``{"document": ...}`` (no separate "record" — L1 sends
exactly one raw page bundle per kept page) and return
``IngestOutcome(accepted, failing_rule)``.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .models import Document


@dataclass
class IngestOutcome:
    accepted: bool
    failing_rule: str | None
    document_id: str | None


def _bundle(doc: Document) -> dict:
    return {"document": doc.model_dump(exclude_none=False)}


class CollectingIngestClient:
    """Captures every sent page bundle (and validates it against the ingest
    rules locally) instead of/in addition to POSTing. Used by the Crawler API
    to return documents inline. If ``forwarders`` are set, each bundle is also
    POSTed to every target Ingest API and the last outcome is authoritative."""

    def __init__(self, forwarders: "list[HttpIngestClient] | None" = None):
        self.forwarders = forwarders or []
        self.collected: list[dict] = []

    def send(self, doc: Document) -> IngestOutcome:
        from ingest_api.validation import validate_page
        docd = doc.model_dump()
        ok, rule = validate_page(docd)
        local = IngestOutcome(ok, rule, doc.document_id)
        fwd_outcomes: list[dict] = []
        for fw in self.forwarders:
            try:
                o = fw.send(doc)
                fwd_outcomes.append({"url": fw.base_url, "accepted": o.accepted, "failing_rule": o.failing_rule})
            except Exception as exc:
                fwd_outcomes.append({"url": fw.base_url, "accepted": False, "failing_rule": f"transport_error:{exc}"})
        outcome = fwd_outcomes[-1] if fwd_outcomes else None
        # A page counts as accepted if ANY forwarder accepted it (or, with no forwarders,
        # local validation passed). Previously we returned `local` regardless, so batches
        # that forwarded to L2 reported accepted=0 even when L2 stored every page.
        any_accepted = (any(o["accepted"] for o in fwd_outcomes)
                        if fwd_outcomes else local.accepted)
        self.collected.append({
            "document_id": doc.document_id,
            "document": docd,
            "accepted": any_accepted,
            "failing_rule": outcome["failing_rule"] if (outcome and not any_accepted) else local.failing_rule,
            "forwarded_to": [o["url"] for o in fwd_outcomes if o["accepted"]],
        })
        return IngestOutcome(any_accepted, local.failing_rule, doc.document_id)


class HttpIngestClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or config.INGEST_BASE_URL).rstrip("/")

    def send(self, doc: Document) -> IngestOutcome:
        import httpx
        url = f"{self.base_url}{config.INGEST_API_PREFIX}/page"
        try:
            resp = httpx.post(url, json=_bundle(doc), timeout=30)
        except Exception as exc:  # noqa: BLE001
            return IngestOutcome(False, f"transport_error:{exc}", None)
        if resp.status_code == 200:
            return IngestOutcome(True, None, resp.json().get("document_id"))
        try:
            fr = resp.json().get("failing_rule")
        except Exception:  # noqa: BLE001
            fr = f"http_{resp.status_code}"
        return IngestOutcome(False, fr, None)


class InProcessIngestClient:
    """Posts through the ASGI app directly (no network)."""

    def __init__(self):
        from fastapi.testclient import TestClient

        from ingest_api.app import app
        self._client = TestClient(app)

    def send(self, doc: Document) -> IngestOutcome:
        url = f"{config.INGEST_API_PREFIX}/page"
        resp = self._client.post(url, json=_bundle(doc))
        if resp.status_code == 200:
            return IngestOutcome(True, None, resp.json().get("document_id"))
        try:
            fr = resp.json().get("failing_rule")
        except Exception:  # noqa: BLE001
            fr = f"http_{resp.status_code}"
        return IngestOutcome(False, fr, None)
