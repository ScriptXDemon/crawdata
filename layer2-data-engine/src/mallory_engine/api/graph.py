"""Graph endpoints (additive Interface B) — read-only lookups over the knowledge graph.

Doctrine: handlers may index-lookup kg_*/srv_* — anything scored or ranked is precomputed
(insights, alliance payload). Ego and path are plain BFS over indexed edges, depth-capped.
"""

from __future__ import annotations

from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models.graph import KgEdge, KgNode, SrvAllianceGraph, SrvGraphInsight

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])

_MAX_NODES = 150


@router.get("/alliances", summary="Prebuilt alliance network payload for the client view")
def alliances(db: Session = Depends(get_db)) -> dict:
    row = db.get(SrvAllianceGraph, "latest")
    if not row:
        raise HTTPException(404, "graph not built yet — POST /ops/rebuild-graph")
    return {"generated_at": row.generated_at, "nodes": row.nodes or [],
            "edges": row.edges or [], "stats": row.stats or {}}


@router.get("/insights", summary="Hidden-pattern insight cards")
def insights(db: Session = Depends(get_db), kind: str | None = None) -> list[dict]:
    stmt = select(SrvGraphInsight).order_by(SrvGraphInsight.rank)
    if kind:
        stmt = stmt.where(SrvGraphInsight.kind == kind)
    return [
        {"id": r.id, "kind": r.kind, "dir": r.dir, "rank": r.rank, "title": r.title,
         "sowhat": r.sowhat, "entities": r.entities, "metric": float(r.metric or 0),
         "provenance": r.provenance}
        for r in db.scalars(stmt).all()
    ]


def _neighbors(db: Session, node_ids: set[str], rels: list[str] | None) -> list[KgEdge]:
    stmt = select(KgEdge).where(
        or_(KgEdge.src_id.in_(node_ids), KgEdge.dst_id.in_(node_ids))
    )
    if rels:
        stmt = stmt.where(KgEdge.rel.in_(rels))
    return list(db.scalars(stmt).all())


def _node_dicts(db: Session, ids: set[str]) -> list[dict]:
    rows = db.scalars(select(KgNode).where(KgNode.id.in_(ids))).all()
    return [{"id": n.id, "kind": n.kind, "label": n.label, "attrs": n.attrs,
             "community": n.community_id, "provenance": n.provenance} for n in rows]


@router.get("/ego", summary="Ego network around one node (undirected BFS, depth <= 3)")
def ego(
    node: str = Query(..., description="node id, e.g. competitor:LT"),
    depth: int = Query(2, ge=1, le=3),
    rels: str | None = Query(None, description="comma-separated rel filter"),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(KgNode, node) is None:
        raise HTTPException(404, f"unknown node {node}")
    rel_list = [r.strip() for r in rels.split(",")] if rels else None

    seen = {node}
    frontier = {node}
    edges_out: list[KgEdge] = []
    for _ in range(depth):
        if not frontier or len(seen) >= _MAX_NODES:
            break
        found = _neighbors(db, frontier, rel_list)
        edges_out.extend(found)
        nxt = {e.dst_id for e in found} | {e.src_id for e in found}
        frontier = (nxt - seen)
        seen |= frontier
        if len(seen) > _MAX_NODES:
            seen = set(list(seen)[:_MAX_NODES])

    uniq = {(e.src_id, e.dst_id, e.rel, e.rel_subtype): e for e in edges_out
            if e.src_id in seen and e.dst_id in seen}
    return {
        "center": node,
        "nodes": _node_dicts(db, seen),
        "edges": [{"src": e.src_id, "dst": e.dst_id, "rel": e.rel, "subtype": e.rel_subtype,
                   "provenance": e.provenance, "confidence": e.confidence, "attrs": e.attrs}
                  for e in uniq.values()],
    }


@router.get("/path", summary="Shortest evidence-bearing path between two nodes")
def path(src: str, dst: str, max_depth: int = Query(4, ge=1, le=6),
         db: Session = Depends(get_db)) -> dict:
    if db.get(KgNode, src) is None or db.get(KgNode, dst) is None:
        raise HTTPException(404, "unknown src or dst node")

    # BFS with parent tracking (undirected).
    parent: dict[str, tuple[str, KgEdge] | None] = {src: None}
    q: deque[tuple[str, int]] = deque([(src, 0)])
    while q:
        cur, d = q.popleft()
        if cur == dst:
            break
        if d >= max_depth:
            continue
        for e in _neighbors(db, {cur}, None):
            other = e.dst_id if e.src_id == cur else e.src_id
            if other not in parent:
                parent[other] = (cur, e)
                q.append((other, d + 1))

    if dst not in parent:
        return {"src": src, "dst": dst, "found": False, "nodes": [], "edges": []}

    hops: list[KgEdge] = []
    node_ids = {dst}
    cur = dst
    while parent[cur] is not None:
        prev, e = parent[cur]
        hops.append(e)
        node_ids.add(prev)
        cur = prev
    hops.reverse()
    return {
        "src": src, "dst": dst, "found": True,
        "nodes": _node_dicts(db, node_ids),
        "edges": [{"src": e.src_id, "dst": e.dst_id, "rel": e.rel, "subtype": e.rel_subtype,
                   "provenance": e.provenance} for e in hops],
    }
