"""Knowledge graph — projection correctness, idempotency, pattern mining, graph API."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from mallory_engine.db import get_db
from mallory_engine.models.graph import KgEdge, KgNode, SrvGraphInsight
from mallory_engine.models.reference import RefCompetitor, RefCompetitorProduct, RefKsslProduct
from mallory_engine.models.serving import SrvGeoEntry, SrvPartnership, SrvTender, SrvTenderMatch
from mallory_engine.services import graph_analytics, graph_builder


def _field(db: Session) -> None:
    """A small competitive field: 2 rivals sharing a partner, contested country, open tender."""
    db.add(RefCompetitor(id="KSSL", name="KSSL", is_anchor=True))
    db.add(RefCompetitor(id="ADANI", name="Adani", is_anchor=False, threat_level="threat"))
    db.add(RefCompetitor(id="NIBE", name="NIBE", is_anchor=False, threat_level="watch"))
    db.add(RefKsslProduct(id="ATAGS", name="ATAGS", category_id="artillery"))
    db.add(RefCompetitorProduct(id="ADANI_gun", competitor_id="ADANI", name="Adani Gun",
                                category_id="artillery"))
    # shared partner: Elbit underpins both rivals
    db.add(SrvPartnership(id=1, competitor_id="ADANI", competitor_name="Adani",
                          partner_name="Elbit", rel_type="license", provenance="sourced"))
    db.add(SrvPartnership(id=2, competitor_id="NIBE", competitor_name="NIBE",
                          partner_name="Elbit", rel_type="license", provenance="sourced"))
    # Adani active in Armenia
    db.add(SrvGeoEntry(id=1, competitor_id="ADANI", competitor_name="Adani",
                       country="Armenia", product_name="Adani Gun", stage="Offered",
                       provenance="sourced"))
    # open artillery tender in Armenia, KSSL fit scored
    db.add(SrvTender(id=1, title="Armenia 155mm artillery tender", country="Armenia",
                     category="artillery", lean="go", status="open", provenance="sourced"))
    db.add(SrvTenderMatch(tender_id=1, kssl_product_id="ATAGS", kssl_product_name="ATAGS",
                          fit_level="high", fit_pct=88))
    db.commit()


def test_projection_derives_expected_nodes_and_edges(db: Session) -> None:
    _field(db)
    counts = graph_builder.rebuild_graph(db)
    db.commit()
    assert counts["nodes"] >= 8  # 3 competitors, 2 products, org, country, tender
    rels = {(e.src_id, e.rel, e.dst_id) for e in db.scalars(select(KgEdge)).all()}
    assert ("competitor:KSSL", "makes", "product:ATAGS") in rels
    assert ("competitor:ADANI", "partners_with", "org:elbit") in rels
    assert ("competitor:ADANI", "present_in", "country:armenia") in rels
    assert ("tender:1", "issued_in", "country:armenia") in rels
    assert ("product:ATAGS", "fits", "tender:1") in rels


def test_rebuild_is_idempotent(db: Session) -> None:
    _field(db)
    c1 = graph_builder.rebuild_graph(db)
    db.commit()
    c2 = graph_builder.rebuild_graph(db)
    db.commit()
    assert c1 == c2
    assert db.query(KgNode).count() == c2["nodes"]


def test_analytics_finds_patterns(db: Session) -> None:
    _field(db)
    graph_builder.rebuild_graph(db)
    res = graph_analytics.run_analytics(db)
    db.commit()

    kinds = {r.kind: r for r in db.scalars(select(SrvGraphInsight)).all()}
    # shared partner: Elbit underpins 2 rivals
    assert "shared_partner" in kinds
    assert "Elbit" in kinds["shared_partner"].title
    # predicted bidder: Adani makes in-category product AND is present in Armenia
    assert "predicted_bidder" in kinds
    assert "competitor:ADANI" in kinds["predicted_bidder"].entities
    assert kinds["predicted_bidder"].dir == "threat"
    # communities assigned on alliance nodes
    elbit = db.get(KgNode, "org:elbit")
    assert elbit.community_id is not None
    assert res["insights"] >= 2


def _client(db: Session) -> TestClient:
    from mallory_engine.api.graph import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_ego_and_path_endpoints(db: Session) -> None:
    _field(db)
    graph_builder.rebuild_graph(db)
    graph_analytics.run_analytics(db)
    db.commit()
    client = _client(db)

    ego = client.get("/api/v1/graph/ego", params={"node": "competitor:ADANI", "depth": 2})
    assert ego.status_code == 200
    ids = {n["id"] for n in ego.json()["nodes"]}
    assert {"competitor:ADANI", "org:elbit", "country:armenia"} <= ids
    # depth 2 reaches NIBE through the shared partner
    assert "competitor:NIBE" in ids

    # how are ADANI and NIBE connected? → via Elbit, 2 hops
    path = client.get("/api/v1/graph/path",
                      params={"src": "competitor:ADANI", "dst": "competitor:NIBE"})
    body = path.json()
    assert body["found"] is True
    assert len(body["edges"]) == 2
    assert any(n["id"] == "org:elbit" for n in body["nodes"])

    alliances = client.get("/api/v1/graph/alliances")
    assert alliances.status_code == 200
    assert alliances.json()["stats"]["nodes"] >= 3

    insights = client.get("/api/v1/graph/insights")
    assert insights.status_code == 200
    assert len(insights.json()) >= 2


def test_ego_unknown_node_404(db: Session) -> None:
    client = _client(db)
    assert client.get("/api/v1/graph/ego", params={"node": "competitor:GHOST"}).status_code == 404
