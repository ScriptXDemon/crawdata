"""Spec-slot extraction — shared by tender scoring, matchups, and (later) multimodal PDF specs.

Slots are data-driven: ``seed_data/spec_slots.json`` can extend them without code edits; the
built-in table below is the fallback. Pulled out of tender_scoring.py so every engine maps
labels → normalized slots the same way.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..config import get_settings

# Built-in slots (the original tender_scoring table). spec_slots.json extends/overrides.
_BUILTIN_SLOTS: dict[str, dict] = {
    "range_km": {"keywords": ["range"], "unit": "km", "polarity": "higher_better"},
    "weight_t": {"keywords": ["weight", "mass"], "unit": "t", "polarity": "lower_better"},
    "calibre_mm": {"keywords": ["calibre", "caliber", "system", "gun"], "unit": "mm",
                   "polarity": "match"},
}


@lru_cache
def slot_table() -> dict[str, dict]:
    table = dict(_BUILTIN_SLOTS)
    path = Path(get_settings().seed_dir) / "spec_slots.json"
    if path.exists():
        try:
            for slot, cfg in json.loads(path.read_text(encoding="utf-8")).get("slots", {}).items():
                table[slot] = cfg
        except Exception:
            pass  # bad seed file must not break scoring; builtin table stands
    return table


def slot_for(label: str) -> str | None:
    low = label.lower()
    for slot, cfg in slot_table().items():
        if any(k in low for k in cfg["keywords"]):
            return slot
    return None


def unit_for(slot: str) -> str:
    return slot_table().get(slot, {}).get("unit", "")


def polarity_for(slot: str) -> str:
    return slot_table().get(slot, {}).get("polarity", "match")


def first_number(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    return float(m.group(1)) if m else None


def required_op(text: str) -> str:
    t = text.lower()
    if any(s in t for s in ("≥", ">=", "at least", "min", "minimum", "exceed")):
        return ">="
    if any(s in t for s in ("≤", "<=", "max", "maximum", "under", "less than", "<")):
        return "<="
    return "=="


def parse_requirements(fields: list[dict] | None) -> dict[str, tuple[str, float]]:
    """Return {slot: (op, value)} extracted from requirement fields."""
    out: dict[str, tuple[str, float]] = {}
    for f in fields or []:
        slot = slot_for(f.get("label", ""))
        val = first_number(f.get("value", ""))
        if slot and val is not None:
            out[slot] = (required_op(f.get("value", "")), val)
    return out
