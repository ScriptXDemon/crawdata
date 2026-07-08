"""The ingest contract is the firewall: valid crawler records parse, malformed ones are rejected."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mallory_engine.contracts.ingest import PageEnvelopeIn


def test_valid_page_envelope_parses() -> None:
    env = PageEnvelopeIn.model_validate(
        {
            "document": {
                "url": "https://x.test/a",
                "content_hash": "sha256:1",
                "fetched_at": "2026-06-28T09:00:00Z",
                "source_id": "JANES",
                "title": "t",
                "main_text": "body",
            },
            "signals": [
                {"stream": "competitive", "competitor_id": "LT", "event_summary": "won a deal"}
            ],
        }
    )
    assert env.document.url == "https://x.test/a"
    assert env.signals[0].stream == "competitive"


def test_document_requires_main_text() -> None:
    with pytest.raises(ValidationError):
        PageEnvelopeIn.model_validate(
            {
                "document": {
                    "url": "https://x.test/a",
                    "content_hash": "sha256:1",
                    "fetched_at": "2026-06-28T09:00:00Z",
                    "source_id": "JANES",
                    "title": "t",
                }
            }
        )


def test_signal_stream_enum_is_enforced() -> None:
    with pytest.raises(ValidationError):
        PageEnvelopeIn.model_validate(
            {
                "document": {
                    "url": "https://x.test/a",
                    "content_hash": "sha256:1",
                    "fetched_at": "2026-06-28T09:00:00Z",
                    "source_id": "JANES",
                    "title": "t",
                    "main_text": "b",
                },
                "signals": [{"stream": "gossip", "event_summary": "x"}],
            }
        )
