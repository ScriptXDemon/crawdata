"""Deterministic, rule-based LLM fallback — no API key, fully offline.

Output is meaningful and KSSL-framed, not lorem. This is the source of truth for the
``LLM_PROVIDER=stub`` path and the per-task fallback every real provider degrades to.
"""

from __future__ import annotations

ANCHOR = "KSSL"

_THREAT_WORDS = ("win", "won", "award", "acquire", "secures", "selected", "contract", "order")
_FAV_WORDS = ("delay", "fails", "lost", "disqualified", "setback", "grounded")
_OPENING_WORDS = ("tender", "rfp", "rfi", "seeks", "plans to buy", "budget", "closing")

_LENS_BY_STREAM = {
    "competitive": "BENCHMARK",
    "market": "MARKET / DEMAND",
    "technology": "TECH MIGRATION",
}


class StubLLMProvider:
    """Rule-based, fully deterministic. Output is meaningful and KSSL-framed, not lorem."""

    def classify_signal(self, *, stream: str, event_summary: str, threat_level: str | None) -> dict:
        text = event_summary.lower()
        if any(w in text for w in _FAV_WORDS):
            direction = "fav"
        elif stream == "market" and any(w in text for w in _OPENING_WORDS):
            direction = "watch"
        elif any(w in text for w in _THREAT_WORDS):
            direction = "threat"
        else:
            direction = threat_level or "watch"

        tags = [direction]
        if any(w in text for w in _OPENING_WORDS):
            tags.append("opening" if stream == "market" else "atstake")
        if "closing" in text or "deadline" in text:
            tags.append("deadline")

        return {"dir": direction, "lens": _LENS_BY_STREAM.get(stream, "BENCHMARK"), "tags": tags}

    def enrich_signal(self, *, stream: str, event_summary: str, company: str | None,
                      dir: str, facts: list[list[str]]) -> dict:
        who = company or "A competitor"
        stance = {
            "threat": f"This strengthens {who} on a line {ANCHOR} contests — expect direct pressure on "
                      f"{ANCHOR}'s bids and pricing.",
            "watch": f"Not an immediate hit to {ANCHOR}, but it shifts the field {ANCHOR} competes in — "
                     "monitor for follow-through.",
            "fav": f"A stumble for {who} opens room for {ANCHOR} to press its indigenous-IP and "
                   "delivery advantage.",
        }[dir]
        return {
            "sowhat": stance,
            "what_text": event_summary,
            "why_text": f"Read against {ANCHOR}'s portfolio, this moves the competitive balance in "
                        f"{stream}. {stance}",
            "lens_reads": [
                [_LENS_BY_STREAM.get(stream, "BENCHMARK"), stance],
                ["POLICY / OFFSET",
                 f"Indigenous-content and offset rules remain {ANCHOR}'s structural lever here."],
            ],
            "actions": [
                ["Counter", f"Brief {ANCHOR} BD on a positioning response within the week."],
                ["Benchmark", f"Compare the named capability against {ANCHOR}'s equivalent product."],
            ],
            "suggest": [
                f"What does this mean for {ANCHOR}?",
                "Who else is affected?",
                "Show the head-to-head.",
            ],
        }

    def tender_verdict(self, *, title: str, best_fit_pct: int, match_summary: str) -> dict:
        if best_fit_pct >= 80:
            lean, head = "go", "Strong fit, pursue."
        elif best_fit_pct >= 55:
            lean, head = "maybe", "Partial fit — qualify before committing."
        else:
            lean, head = "pass", "Weak fit — monitor only."
        return {
            "lean": lean,
            "lean_text": f"<b>{head}</b> Best {ANCHOR} match scores {best_fit_pct}%. {match_summary}",
        }

    def chat(self, *, system: str, context: str, message: str) -> str:
        # Deterministic, grounded fallback: surface the scoped context honestly.
        if not context.strip():
            return (
                f"I don't have data in view for that yet. Ask me about a selected signal, tender, "
                f"or competitor and I'll answer from {ANCHOR}'s intelligence."
            )
        head = context.strip().splitlines()[0]
        return (
            f"Based on the data in view: {head} "
            f"For {ANCHOR}, the relevant read is the indigenous-IP, forging-scale and delivery-maturity "
            f"contrast. (Connect an OpenRouter key to enable full Mallory reasoning.)"
        )
