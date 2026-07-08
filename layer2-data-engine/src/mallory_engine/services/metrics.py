"""S-11 Overview metrics builder — pre-compute the metric strip per pillar (client renders no math)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.serving import SrvOverviewMetrics, SrvSignal

_DIR_META = {
    "threat": ("Threats", "var(--threat)"),
    "watch": ("Watch", "var(--watch)"),
    "fav": ("Favourable", "var(--fav)"),
}


def build_overview_metrics(db: Session) -> None:
    rows = db.execute(
        select(SrvSignal.pillar, SrvSignal.dir, func.count()).group_by(
            SrvSignal.pillar, SrvSignal.dir
        )
    ).all()

    counts: dict[str, dict[str, int]] = {}
    for pillar, direction, n in rows:
        counts.setdefault(pillar, {})[direction] = n

    now = dt.datetime.now(tz=dt.timezone.utc)
    for pillar, dir_counts in counts.items():
        metrics = [
            {
                "label": label,
                "value": dir_counts.get(direction, 0),
                "color": color,
                "filter": direction,
            }
            for direction, (label, color) in _DIR_META.items()
        ]
        metrics.append({"label": "Total signals", "value": sum(dir_counts.values()),
                        "color": "var(--ink)", "filter": "all"})
        db.merge(SrvOverviewMetrics(pillar=pillar, generated_at=now, metrics=metrics))
