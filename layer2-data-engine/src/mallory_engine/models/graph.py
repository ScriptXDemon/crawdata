"""Knowledge graph (``kg_*``) — Mallory's Intara analog, plus its serving projections.

``kg_nodes``/``kg_edges`` are a label-property graph in tabular form: a PURE deterministic
projection of the relational system of record (ref_*/srv_*), fully rebuilt by the graph
builder — so incremental bugs self-heal and the graph is always reproducible. Analytics
(centrality, community) write back onto node columns after each rebuild.

Edge evidence reuses ``srv_evidence`` with target_kind='kg_edge' — one uniform XAI chain.
The client reads only the ``srv_*`` projections and the read-only graph endpoints.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class KgNode(Base):
    __tablename__ = "kg_nodes"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # '{kind}:{key}' e.g. 'competitor:LT'
    kind: Mapped[str] = mapped_column(String, index=True)  # competitor|org|product|country|tender|signal|patent
    label: Mapped[str] = mapped_column(String)
    ref_table: Mapped[str | None] = mapped_column(String, nullable=True)  # lineage
    ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attrs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")  # sourced|estimate|analyst
    # Analytics (filled after each rebuild)
    degree: Mapped[int | None] = mapped_column(Integer, nullable=True)
    betweenness: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    eigenvector: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    community_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class KgEdge(Base):
    __tablename__ = "kg_edges"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    src_id: Mapped[str] = mapped_column(String, index=True)
    dst_id: Mapped[str] = mapped_column(String, index=True)
    rel: Mapped[str] = mapped_column(String, index=True)  # makes|partners_with|present_in|competes_with|fits|about|filed|issued_in
    rel_subtype: Mapped[str] = mapped_column(String, default="")  # jv|license|Contracted|...
    weight: Mapped[float] = mapped_column(Numeric, default=1)  # contributing-row count
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")
    attrs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # deal_value, stage, fit_pct...
    first_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SrvGraphInsight(Base):
    """Hidden-pattern cards (same shape as signal cards — they drop into the feed/UI as-is)."""

    __tablename__ = "srv_graph_insights"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    kind: Mapped[str] = mapped_column(String, index=True)  # shared_partner|broker|predicted_bidder|community
    dir: Mapped[str] = mapped_column(String, default="watch")  # threat|watch|fav
    rank: Mapped[int] = mapped_column(Integer, default=999)
    title: Mapped[str] = mapped_column(String)
    sowhat: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # node ids involved
    metric: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="sourced")
    computed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SrvAllianceGraph(Base):
    """One prebuilt node-link payload for the client's alliance/network view."""

    __tablename__ = "srv_alliance_graph"
    id: Mapped[str] = mapped_column(String, primary_key=True, default="latest")
    generated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nodes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    edges: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
