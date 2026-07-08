"""Assistant endpoints — the two write-back proxies into L2 compute (client still computes nothing)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..contracts.serving import MalloryRequest, MalloryResponse, ReportRequest, ReportResponse
from ..db import get_db
from ..services import assistant
from ..services.llm import get_llm

router = APIRouter(prefix="/api/v1", tags=["assistant"])


@router.post("/mallory/chat", response_model=MalloryResponse)
def mallory_chat(req: MalloryRequest, db: Session = Depends(get_db)) -> MalloryResponse:
    return assistant.answer(db, get_llm(), req)


@router.post("/reports/ceo", response_model=ReportResponse)
def ceo_report(req: ReportRequest, db: Session = Depends(get_db)) -> ReportResponse:
    return assistant.ceo_report(db, get_llm(), req.focus)
