"""S-22/23/24 synthesis engines — deterministic cores, LLM prose, fail-safe publishing."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from mallory_engine.models.reference import RefCompetitor, RefMatchup
from mallory_engine.models.serving import (
    SrvCompetitorSynthesis,
    SrvEvidence,
    SrvFieldPattern,
    SrvMatchup,
    SrvMatchupSpec,
    SrvPartnership,
    SrvSignal,
)
from mallory_engine.services import competitor_synthesis, field_patterns, matchup_synthesis

# ── S-22 matchup engine ──


def _ref_matchup(db: Session) -> None:
    db.add(RefMatchup(
        id="ATAGS__caesar", kssl_product_id="ATAGS", kssl_name="ATAGS", comp_name="CAESAR 6x6",
        comp_by="KNDS", country="France", category_id="artillery",
        specs=[
            {"label": "Calibre", "kssl": "155/52", "comp": "155/52"},
            {"label": "Max range", "kssl": "48 km", "comp": "42 km",
             "kssl_num": 48, "comp_num": 42, "better": "high", "highlight": True},
            {"label": "Crew", "kssl": "6", "comp": "3", "kssl_num": 6, "comp_num": 3,
             "better": "low"},
        ],
        adv_kssl=["Indigenous IP"], adv_comp=["Export record"],
    ))
    db.commit()


def test_matchup_recompute_is_deterministic(db: Session) -> None:
    _ref_matchup(db)
    n = matchup_synthesis.recompute_all(db)
    db.commit()
    assert n == 1

    m = db.scalars(select(SrvMatchup)).one()
    # range: kssl leads, highlight → +2; crew: comp leads (lower better) → -1 ⇒ 50 + 12*1 = 62
    assert m.edge_score == 62
    assert m.dir == "fav"
    assert m.verdict  # template verdict without LLM
    assert m.verdict_method == "rule"
    assert len(m.edge_parts) == 3
    specs = db.scalars(select(SrvMatchupSpec)).all()
    leaders = {s.spec_label: s.leader for s in specs}
    assert leaders == {"Calibre": "tie", "Max range": "kssl", "Crew": "comp"}
    # evidence link back to the ref row
    ev = db.scalars(select(SrvEvidence).where(SrvEvidence.target_kind == "matchup")).all()
    assert ev and ev[0].evidence_id == "ref:ref_matchups:ATAGS__caesar"


def test_matchup_recompute_is_idempotent(db: Session) -> None:
    _ref_matchup(db)
    matchup_synthesis.recompute_all(db)
    db.commit()
    matchup_synthesis.recompute_all(db)
    db.commit()
    assert len(db.scalars(select(SrvMatchup)).all()) == 1


def test_matchup_llm_verdict_used_when_valid(db: Session) -> None:
    _ref_matchup(db)

    class FakeLLM:
        def matchup_verdict(self, **kw):
            return {"verdict": "KSSL leads on reach; CAESAR on crew economy."}

    matchup_synthesis.recompute_all(db, FakeLLM())
    db.commit()
    m = db.scalars(select(SrvMatchup)).one()
    assert m.verdict_method == "llm"
    assert "reach" in m.verdict


# ── S-23 competitor synthesis ──


def _competitor_with_signals(db: Session, n_signals: int) -> None:
    db.add(RefCompetitor(id="LT", name="L&T", is_anchor=False))
    for i in range(n_signals):
        db.add(SrvSignal(
            id=i + 1, pillar="competitive", dir="threat", rank=i + 1,
            title=f"L&T event {i + 1}", company="L&T", provenance="sourced",
            confidence=80, confidence_band="high", corroboration=1,
        ))
    db.commit()


def test_synthesis_failsafe_keeps_row_on_thin_evidence(db: Session) -> None:
    _competitor_with_signals(db, 1)  # below the min_evidence=3 floor
    db.add(SrvCompetitorSynthesis(competitor_id="LT", competitor_name="L&T",
                                  thesis="seed thesis", provenance="estimate"))
    db.commit()

    class FakeLLM:
        def synthesize_competitor(self, **kw):
            raise AssertionError("must not be called under the evidence floor")

    res = competitor_synthesis.synthesize_competitor(db, FakeLLM(), "LT")
    assert res["status"] == "kept"
    row = db.get(SrvCompetitorSynthesis, "LT")
    assert row.thesis == "seed thesis" and row.provenance == "estimate"


def test_synthesis_failsafe_keeps_row_on_bad_generation(db: Session) -> None:
    _competitor_with_signals(db, 4)
    db.add(SrvCompetitorSynthesis(competitor_id="LT", competitor_name="L&T",
                                  thesis="seed thesis", provenance="estimate"))
    db.commit()

    class FakeLLM:
        def synthesize_competitor(self, **kw):
            return {}  # generation failed schema/validators

    res = competitor_synthesis.synthesize_competitor(db, FakeLLM(), "LT")
    assert res["status"] == "kept"
    assert db.get(SrvCompetitorSynthesis, "LT").provenance == "estimate"


def test_synthesis_drops_uncited_vulnerabilities(db: Session) -> None:
    """Exemplar/parametric-knowledge claims must not launder into 'sourced' intelligence."""
    _competitor_with_signals(db, 4)

    class FakeLLM:
        def synthesize_competitor(self, **kw):
            return {
                "thesis": "t", "strat_sowhat": "s",
                "vulnerabilities": [
                    {"title": "Cited", "intel": "real", "cites": ["sig:1"]},
                    {"title": "Leaked from exemplar", "intel": "invented", "cites": []},
                    {"title": "Bad cite", "intel": "x", "cites": ["sig:999"]},
                ],
                "cites": ["sig:1"],
            }

    res = competitor_synthesis.synthesize_competitor(db, FakeLLM(), "LT")
    assert res["status"] == "sourced"
    row = db.get(SrvCompetitorSynthesis, "LT")
    assert [v["title"] for v in row.vulnerabilities] == ["Cited"]


def test_synthesis_kept_when_no_vulnerability_survives(db: Session) -> None:
    _competitor_with_signals(db, 4)

    class FakeLLM:
        def synthesize_competitor(self, **kw):
            return {"thesis": "t", "strat_sowhat": "s",
                    "vulnerabilities": [{"title": "Uncited", "intel": "x", "cites": []}],
                    "cites": ["sig:1"]}

    res = competitor_synthesis.synthesize_competitor(db, FakeLLM(), "LT")
    assert res["status"] == "kept"
    assert db.get(SrvCompetitorSynthesis, "LT") is None  # nothing published


def test_synthesis_publishes_sourced_with_evidence(db: Session) -> None:
    _competitor_with_signals(db, 4)

    class FakeLLM:
        def synthesize_competitor(self, **kw):
            assert "[sig:1]" in kw["pack_text"]  # evidence pack rendered with eids
            return {
                "thesis": "L&T executes, but on licensed IP.",
                "strat_sowhat": "Compete on indigenous ownership.",
                "vulnerabilities": [
                    {"title": "Foreign weapon core", "intel": "K9 is Hanwha's design.",
                     "cites": ["sig:1", "sig:2"]},
                ],
                "predictions": ["More follow-on wins."],
                "moves": ["Press the IP contrast."],
                "gaps": ["No pricing evidence."],
                "cites": ["sig:1"],
            }

    res = competitor_synthesis.synthesize_competitor(db, FakeLLM(), "LT")
    assert res["status"] == "sourced"

    row = db.get(SrvCompetitorSynthesis, "LT")
    assert row.provenance == "sourced"
    assert row.confidence == 80  # mean of cited sig confidences
    assert row.gaps == ["No pricing evidence."]
    assert row.updated_at is not None

    ev = db.scalars(select(SrvEvidence).where(SrvEvidence.target_kind == "synthesis")).all()
    fields = {e.field for e in ev}
    assert "thesis" in fields and "vulnerability:0" in fields
    assert all(e.method == "llm" for e in ev)


# ── S-24 field patterns ──


def test_field_patterns_aggregates_and_fallback(db: Session) -> None:
    # Elbit partners with two rivals → shared-partner aggregate; fallback publishes it.
    for i, comp in enumerate(["ADANI", "NIBE"], start=1):
        db.add(SrvPartnership(
            id=i, competitor_id=comp, competitor_name=comp, partner_name="Elbit",
            rel_type="license", provenance="sourced",
        ))
    db.commit()

    aggs = field_patterns.compute_aggregates(db)
    kinds = {a.eid.split(":")[1] for a in aggs}
    assert "shared_partner" in kinds

    res = field_patterns.refresh_field_patterns(db, llm=None)
    db.commit()
    assert res["status"] == "sourced" and res["method"] == "rule"
    rows = db.scalars(select(SrvFieldPattern)).all()
    assert rows and all(r.provenance == "sourced" for r in rows)
    assert any("Elbit" in r.title for r in rows)


def test_field_patterns_kept_when_no_sourced_data(db: Session) -> None:
    res = field_patterns.refresh_field_patterns(db, llm=None)
    assert res["status"] == "kept"
