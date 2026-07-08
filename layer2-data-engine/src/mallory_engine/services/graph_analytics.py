"""Graph analytics — the hidden-pattern miners over kg_nodes/kg_edges (NetworkX, deterministic).

Four analyses, each answering a business question:
  1. Shared partners — "which org is a chokepoint across the field?" (bipartite pile-up)
  2. Communities   — "which alliance blocs exist?" (Louvain, seed=42 → community_id on nodes)
  3. Centrality    — "which org, if courted or disrupted, most changes the network?" (brokers)
  4. Predicted bidders — "who shows up against KSSL on tender T?" (rule-based path inference)

Outputs: insight cards (srv_graph_insights, same shape as signal cards) and the prebuilt
alliance payload (srv_alliance_graph) for the client's network view.
# ponytail: temporal burst detection deferred — needs months of edge volume to mean anything.
"""

from __future__ import annotations

import datetime as dt

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.graph import KgEdge, KgNode, SrvAllianceGraph, SrvGraphInsight


def _alliance_nx(db: Session) -> nx.Graph:
    """Undirected competitor↔org graph over partners_with edges."""
    g = nx.Graph()
    nodes = {n.id: n for n in db.scalars(
        select(KgNode).where(KgNode.kind.in_(["competitor", "org"]))).all()}
    for n in nodes.values():
        g.add_node(n.id, kind=n.kind, label=n.label)
    for e in db.scalars(select(KgEdge).where(KgEdge.rel == "partners_with")).all():
        if e.src_id in nodes and e.dst_id in nodes:
            g.add_edge(e.src_id, e.dst_id, weight=float(e.weight or 1), subtype=e.rel_subtype)
    return g


def run_analytics(db: Session) -> dict:
    """Compute insights + alliance payload. Call after rebuild_graph."""
    g = _alliance_nx(db)
    nodes_by_id = {n.id: n for n in db.scalars(select(KgNode)).all()}
    db.query(SrvGraphInsight).delete()
    now = dt.datetime.now(dt.timezone.utc)
    n_insights = 0

    # 1 ── shared partners: one org underpinning >= 2 competitors
    for nid, data in g.nodes(data=True):
        if data.get("kind") != "org":
            continue
        comps = [x for x in g.neighbors(nid) if g.nodes[x].get("kind") == "competitor"]
        if len(comps) >= 2:
            names = sorted(g.nodes[c]["label"] for c in comps)
            db.add(SrvGraphInsight(
                kind="shared_partner", dir="watch", title=f"{data['label']} underpins {len(comps)} rivals",
                sowhat=(f"{data['label']} has ties to {', '.join(names)} — a shared dependency "
                        "KSSL can exploit or must not depend on."),
                entities=[nid, *comps], metric=float(len(comps)), computed_at=now,
            ))
            n_insights += 1

    # 2 ── communities (alliance blocs)
    stats: dict = {}
    if g.number_of_edges() > 0:
        communities = nx.community.louvain_communities(g, seed=42)
        for cid, members in enumerate(communities):
            for m in members:
                if m in nodes_by_id:
                    nodes_by_id[m].community_id = cid
        blocs = [
            sorted((g.nodes[m]["label"] for m in members
                    if g.nodes[m].get("kind") == "competitor"))
            for members in communities
        ]
        stats["blocs"] = [b for b in blocs if b]

    # 3 ── centrality: degree on all; betweenness/eigenvector on the alliance graph
    if g.number_of_edges() > 0:
        bet = nx.betweenness_centrality(g)
        try:
            eig = nx.eigenvector_centrality(g, max_iter=500)
        except nx.PowerIterationFailedConvergence:
            eig = {}
        for nid in g.nodes:
            if nid in nodes_by_id:
                nodes_by_id[nid].degree = g.degree(nid)
                nodes_by_id[nid].betweenness = round(bet.get(nid, 0.0), 4)
                nodes_by_id[nid].eigenvector = round(eig.get(nid, 0.0), 4)
        brokers = [n for n, v in sorted(bet.items(), key=lambda kv: -kv[1])
                   if g.nodes[n].get("kind") == "org" and v > 0][:1]
        for nid in brokers:
            db.add(SrvGraphInsight(
                kind="broker", dir="watch", title=f"{g.nodes[nid]['label']} is the network's key broker",
                sowhat=f"{g.nodes[nid]['label']} sits between alliance blocs — courting or "
                       "disrupting it most changes the field.",
                entities=[nid], metric=round(bet[nid], 4), computed_at=now,
            ))
            n_insights += 1

    # 4 ── predicted bidders: makes(product in tender category) ∧ present_in(tender country)
    edges = db.scalars(select(KgEdge)).all()
    makes = [(e.src_id, e.dst_id) for e in edges if e.rel == "makes"]
    present = {(e.src_id, e.dst_id) for e in edges if e.rel == "present_in"}
    issued = {e.src_id: e.dst_id for e in edges if e.rel == "issued_in"}  # tender -> country
    for tender_id, country_id in issued.items():
        tender = nodes_by_id.get(tender_id)
        if tender is None or (tender.attrs or {}).get("status") == "closed":
            continue
        category = (tender.attrs or {}).get("category")
        bidders = []
        for comp_id, prod_id in makes:
            prod = nodes_by_id.get(prod_id)
            if prod is None or (prod.attrs or {}).get("side") != "competitor":
                continue
            if category and (prod.attrs or {}).get("category") == category \
                    and (comp_id, country_id) in present:
                bidders.append(comp_id)
        if bidders:
            names = sorted(nodes_by_id[c].label for c in set(bidders) if c in nodes_by_id)
            db.add(SrvGraphInsight(
                kind="predicted_bidder", dir="threat",
                title=f"Likely rivals on: {tender.label[:70]}",
                sowhat=(f"{', '.join(names)} make in-category products AND are active in the "
                        "tender's country — expect them at the table."),
                entities=[tender_id, *sorted(set(bidders))], metric=float(len(set(bidders))),
                computed_at=now,
            ))
            n_insights += 1

    # rank insights: threats first, then by metric
    rows = db.query(SrvGraphInsight).all()
    rows.sort(key=lambda r: ({"threat": 0, "watch": 1, "fav": 2}.get(r.dir, 3), -(r.metric or 0)))
    for i, r in enumerate(rows, start=1):
        r.rank = i

    # 5 ── alliance payload for the client view
    payload_nodes = [
        {"id": n.id, "kind": n.kind, "label": n.label, "community": n.community_id,
         "degree": n.degree, "betweenness": float(n.betweenness or 0),
         "dir": (n.attrs or {}).get("dir"), "is_anchor": (n.attrs or {}).get("is_anchor", False)}
        for n in nodes_by_id.values() if n.kind in ("competitor", "org")
    ]
    payload_edges = [
        {"src": e.src_id, "dst": e.dst_id, "rel": e.rel_subtype or "partnership",
         "provenance": e.provenance}
        for e in edges if e.rel == "partners_with"
    ]
    stats.update({"nodes": len(payload_nodes), "edges": len(payload_edges),
                  "insights": n_insights})
    db.merge(SrvAllianceGraph(id="latest", generated_at=now, nodes=payload_nodes,
                              edges=payload_edges, stats=stats))
    return {"insights": n_insights, **stats}
