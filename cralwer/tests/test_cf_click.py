"""The simple solution to the interactive Cloudflare challenge: a coordinate click.

An interactive Turnstile ("Verify you are human") never self-clears, and its checkbox is inside a
cross-origin challenges.cloudflare.com iframe that no selector can reach. The CamoFox /click endpoint
only accepted ref/selector, so it was unclickable — which is why C3 sat on the interstitial for 75s+
and recorded the page as blocked. A small server patch wires page.mouse (a TRUSTED, humanized click)
to a pixel coordinate; clicking the checkbox that way clears the challenge (verified live on
scrapingcourse.com/antibot-challenge: "You bypassed the Antibot challenge!").

These tests pin the client behaviour and the presence of the server patch without needing a browser.

Runnable directly:  python tests/test_cf_click.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import camofox_client as cc  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Records posts and answers the evaluate (widget box) then the click."""

    def __init__(self, slot, click_ok=True):
        self.slot = slot
        self.click_ok = click_ok
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json))
        if url.endswith("/evaluate"):
            return _FakeResp(200, {"result": self.slot})
        if url.endswith("/click"):
            if not self.click_ok:                       # unpatched server: coordinates rejected
                return _FakeResp(400, {"error": "ref or selector required"})
            coords = (json or {}).get("coordinates") or {}
            return _FakeResp(200, {"clicked": True, "method": "coordinates",
                                   "x": coords.get("x"), "y": coords.get("y")})
        return _FakeResp(200, {})


def t_checkbox_click_targets_the_left_of_the_widget() -> None:
    """The checkbox renders ~30px in from the widget's left edge, vertically centred. A click at the
    widget's centre would miss it (that's the label/logo), so the offset matters."""
    slot = {"x": 832, "y": 304, "h": 69}
    c = _FakeClient(slot)
    assert cc._click_turnstile_checkbox(c, "http://x", "u", "tab") is True
    click = next(j for url, j in c.calls if url.endswith("/click"))
    x, y = click["coordinates"]["x"], click["coordinates"]["y"]
    assert x == 862, f"x={x}: not aimed at the checkbox (left edge + 30)"
    assert y == 338, f"y={y}: not vertically centred in the widget"


def t_a_coordinate_click_is_sent_not_a_selector() -> None:
    """The whole point: the checkbox is in a cross-origin iframe, so it must be a coordinate click.
    A regression back to selector/body clicking would silently stop solving Cloudflare."""
    c = _FakeClient({"x": 500, "y": 300, "h": 60})
    cc._click_turnstile_checkbox(c, "http://x", "u", "tab")
    click = next(j for url, j in c.calls if url.endswith("/click"))
    assert "coordinates" in click and "selector" not in click and "ref" not in click


def t_unpatched_server_is_a_safe_no_op() -> None:
    """On a CamoFox without the coordinate patch, /click 400s. The solver must report failure
    quietly, never raise — a crawl on an old browser image must not crash here."""
    c = _FakeClient({"x": 500, "y": 300, "h": 60}, click_ok=False)
    assert cc._click_turnstile_checkbox(c, "http://x", "u", "tab") is False


def t_no_widget_found_is_a_safe_no_op() -> None:
    c = _FakeClient(None)
    assert cc._click_turnstile_checkbox(c, "http://x", "u", "tab") is False
    # ...and it must not have attempted a click with no target.
    assert not any(url.endswith("/click") for url, _ in c.calls)


def t_the_server_patch_is_present_in_both_vendor_copies() -> None:
    """The client sends coordinates, but they only work if the CamoFox server accepts them. Both
    build sources must carry the patch or a rebuilt image silently loses the capability."""
    for rel in ("vendor/camofox-browser/server.js",
                "Production crawler/vendor/camofox-browser/server.js"):
        src = (_REPO / rel).read_text(encoding="utf-8", errors="ignore")
        assert "coordinate click dispatched" in src, f"{rel} lost the coordinate-click patch"
        assert "ref, selector, or coordinates required" in src, f"{rel} still rejects coordinates"
        # the humanized approach (not a teleport) is what makes the click read as human
        assert "curved approach" in src, f"{rel} lost the humanized mouse path"


def t_the_settle_loop_actually_calls_the_checkbox_click() -> None:
    """Wiring check: an interactive challenge only clears if _settle drives the click. If a refactor
    dropped the call, Cloudflare would silently regress to unsolved."""
    import inspect
    src = inspect.getsource(cc._settle)
    assert "_click_turnstile_checkbox" in src
    assert "CRAWLER_CAMOFOX_CF_CLICK" in src, "the disable-gate is gone"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CF-CLICK TESTS PASSED")


def test_cf_click_suite() -> None:
    main()


if __name__ == "__main__":
    main()
