"""Internal ops endpoints — run the pipeline and inspect processing state (the monitor's data)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models.serving import SrvSignal, SrvTender
from ..models.staging import StgSignal, StgTender
from ..pipeline.runner import process_pending
from ..services import (
    competitor_synthesis,
    field_patterns,
    graph_analytics,
    graph_builder,
    matchup_synthesis,
    multimodal,
)
from ..services.llm import get_llm

router = APIRouter(prefix="/ops", tags=["ops"])


@router.post("/process", summary="Run the pipeline over pending staging rows")
def run_pipeline(db: Session = Depends(get_db)) -> dict:
    result = process_pending(db)
    return {
        "signals_processed": result.signals_processed,
        "tenders_processed": result.tenders_processed,
        "partnerships_processed": result.partnerships_processed,
        "geo_processed": result.geo_processed,
        "innovation_processed": result.innovation_processed,
    }


@router.post("/rebuild-graph", summary="Rebuild the knowledge graph + run pattern analytics")
def rebuild_graph(db: Session = Depends(get_db)) -> dict:
    counts = graph_builder.rebuild_graph(db)
    analytics = graph_analytics.run_analytics(db)
    db.commit()
    return {**counts, **analytics}


@router.post("/recompute-matchups", summary="S-22: rebuild srv_matchups from ref_matchups")
def recompute_matchups(db: Session = Depends(get_db)) -> dict:
    n = matchup_synthesis.recompute_all(db, get_llm(db=db))
    db.commit()
    return {"matchups": n}


@router.post("/synthesize", summary="S-23: competitor synthesis (all, or one via ?competitor=)")
def synthesize(competitor: str | None = None, db: Session = Depends(get_db)) -> dict:
    llm = get_llm(db=db)
    if competitor:
        results = [competitor_synthesis.synthesize_competitor(db, llm, competitor)]
    else:
        results = competitor_synthesis.synthesize_all(db, llm)
    db.commit()
    return {"results": results}


@router.post("/field-patterns", summary="S-24: recompute cross-field patterns")
def refresh_patterns(db: Session = Depends(get_db)) -> dict:
    result = field_patterns.refresh_field_patterns(db, get_llm(db=db))
    db.commit()
    return result


@router.post("/analyze-assets", summary="Multimodal: caption images + extract PDF specs (vision swaps in)")
def analyze_assets(db: Session = Depends(get_db)) -> dict:
    # opt-in because the vision model must swap into VRAM; keep it off the hot path.
    return multimodal.analyze_pending_assets(db, get_llm(db=db))


# ── LLM backend: live farm ⇄ local switch (no restart) ─────────────────────────
# Two presets. get_llm() rebuilds its transport from the settings singleton on every call,
# so mutating that singleton flips the backend for the very next pipeline run.
# ponytail: two hardcoded presets — there are exactly two brains; a config table would be
# ceremony. host.docker.internal is the container's route to the host's local Ollama.
_LLM_PRESETS = {
    "farm": {"base_url": "https://ollama.i3softlab.com/v1",
             "fast": "text-model", "deep": "text-model", "vision": "vlm-model"},
    # deep=7b too: on a 24GB GPU, 14b at 32k ctx spills to CPU (~2x slower per call) and the
    # enrichment loop is sequential — that combo turns a backfill into hours. 7b fits fully in
    # VRAM and is fast; re-enrich high-value rows with 14b later if wanted.
    # ponytail: raise deep back to 14b (with capped num_ctx) once enrichment is parallelized.
    "local": {"base_url": "http://host.docker.internal:11434/v1",
              "fast": "qwen2.5:7b", "deep": "qwen2.5:7b", "vision": "qwen2.5vl:7b"},
}


def _llm_mode(s) -> str:
    return "farm" if "i3softlab" in (s.ollama_base_url or "") else "local"


def _ping(base_url: str, api_key: str) -> dict:
    """Quick GET {base}/models — is this backend reachable right now?"""
    import time

    import httpx  # already a dependency (the transport uses it)

    h = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    t0 = time.perf_counter()
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/models", headers=h, timeout=6)
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code == 200:
            return {"ok": True, "ms": ms,
                    "models": [m.get("id") for m in r.json().get("data", [])]}
        return {"ok": False, "ms": ms, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


@router.get("/llm", summary="Current LLM backend + live farm/local health")
def llm_status() -> dict:
    s = get_settings()
    return {
        "mode": _llm_mode(s),
        "base_url": s.ollama_base_url,
        "models": {"fast": s.ollama_model_fast, "deep": s.ollama_model_deep,
                   "vision": s.ollama_model_vision},
        "has_key": bool(s.ollama_api_key),
        "health": {
            "farm": _ping(_LLM_PRESETS["farm"]["base_url"], s.ollama_api_key),
            "local": _ping(_LLM_PRESETS["local"]["base_url"], ""),
        },
    }


@router.post("/llm/switch", summary="Live-switch LLM backend farm⇄local (no restart)")
def llm_switch(mode: str) -> dict:
    if mode not in _LLM_PRESETS:
        raise HTTPException(400, f"mode must be 'farm' or 'local', got {mode!r}")
    s = get_settings()
    p = _LLM_PRESETS[mode]
    # Mutate the cached singleton in place — GIL-atomic attribute writes, no lock; a race
    # between two rapid toggles self-heals on the next switch. ponytail: per-request lock
    # only if concurrent toggling ever matters (it won't — one operator, one dashboard).
    s.ollama_base_url = p["base_url"]
    s.ollama_model_fast = p["fast"]
    s.ollama_model_deep = p["deep"]
    s.ollama_model_vision = p["vision"]
    s.llm_provider = "ollama"  # never leave it on the stub after an explicit switch
    key = s.ollama_api_key if mode == "farm" else ""
    return {"switched_to": mode, "base_url": s.ollama_base_url,
            "models": {"fast": p["fast"], "deep": p["deep"], "vision": p["vision"]},
            "ping": _ping(p["base_url"], key)}


@router.get("/status", summary="Processing-state counts (feeds the monitor view)")
def status(db: Session = Depends(get_db)) -> dict:
    def by_status(model) -> dict[str, int]:
        rows = db.execute(
            select(model.proc_status, func.count()).group_by(model.proc_status)
        ).all()
        return {s: n for s, n in rows}

    return {
        "staging": {"signals": by_status(StgSignal), "tenders": by_status(StgTender)},
        "serving": {
            "signals": db.scalar(select(func.count()).select_from(SrvSignal)) or 0,
            "tenders": db.scalar(select(func.count()).select_from(SrvTender)) or 0,
        },
    }
