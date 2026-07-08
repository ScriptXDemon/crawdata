"""Reference (``ref_*``) models — the static "vs KSSL" baseline, loaded from seed JSON."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class RefCategory(Base):
    __tablename__ = "ref_categories"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'artillery'
    name: Mapped[str] = mapped_column(String)


class RefCountry(Base):
    __tablename__ = "ref_countries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    region: Mapped[str | None] = mapped_column(String, nullable=True)


class RefTechDomain(Base):
    __tablename__ = "ref_tech_domains"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'artillery'
    name: Mapped[str] = mapped_column(String)
    keywords: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class RefCompetitor(Base):
    __tablename__ = "ref_competitors"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'LT'
    name: Mapped[str] = mapped_column(String)
    aliases: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    hq_country: Mapped[str | None] = mapped_column(String, nullable=True)
    threat_level: Mapped[str | None] = mapped_column(String, nullable=True)  # threat|watch|fav
    is_anchor: Mapped[bool] = mapped_column(Boolean, default=False)  # true only for KSSL
    priority: Mapped[str | None] = mapped_column(String, nullable=True)  # P1|P2|P3


class RefKsslProduct(Base):
    __tablename__ = "ref_kssl_products"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'ATAGS'
    name: Mapped[str] = mapped_column(String)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("ref_categories.id"), nullable=True)
    aliases: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class RefCompetitorProduct(Base):
    __tablename__ = "ref_competitor_products"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'CAESAR6x6'
    competitor_id: Mapped[str] = mapped_column(ForeignKey("ref_competitors.id"))
    name: Mapped[str] = mapped_column(String)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("ref_categories.id"), nullable=True)
    aliases: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class RefMatchup(Base):
    """Which KSSL product is benchmarked against which competitor product (admin-curated).

    The serving rows (srv_matchups) are RECOMPUTED from these + ref_product_specs by the
    S-22 matchup engine — never hand-written.
    """

    __tablename__ = "ref_matchups"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # 'ATAGS__caesar_6x6'
    kssl_product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kssl_name: Mapped[str] = mapped_column(String)
    comp_name: Mapped[str] = mapped_column(String)
    comp_by: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    category_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Paired spec rows exactly as curated: [{label, kssl, comp, kssl_num?, comp_num?, better?, highlight?}]
    specs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    adv_kssl: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # curated context lines
    adv_comp: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class RefProductSpec(Base):
    """One spec row per (product, attribute). Works for both KSSL and competitor products."""

    __tablename__ = "ref_product_specs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String, index=True)
    product_side: Mapped[str] = mapped_column(String)  # 'kssl' | 'competitor'
    spec_label: Mapped[str] = mapped_column(String)  # 'Max range'
    value_text: Mapped[str | None] = mapped_column(String, nullable=True)  # '40+'
    value_num: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # 40.0
    unit: Mapped[str | None] = mapped_column(String, nullable=True)  # 'km'
    polarity: Mapped[str | None] = mapped_column(String, nullable=True)  # higher_better|lower_better
    is_highlight: Mapped[bool] = mapped_column(Boolean, default=False)
