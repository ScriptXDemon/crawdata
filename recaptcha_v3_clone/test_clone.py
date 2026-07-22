"""Self-check for the reCAPTCHA v3 clone. No running server needed — imports the scorer and token
functions directly and asserts the properties the write-up claims.

The point of the clone is to make an argument (Part 3.4: a client-side collector can always be
lied to). These asserts pin the two halves of that argument in code:
  - the honest signals the collector was designed to catch (bot vs human) ARE separated, and
  - the forged bundle — a lie the server cannot distinguish from truth — sails through.

Runnable directly:  python test_clone.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server                      # noqa: E402
from forge import forged_bundle    # noqa: E402


def _bot() -> dict:
    return {"sitekey": "clone-site-key-123", "action": "submit_contact",
            "timeToExecuteMs": 40, "dwellMs": 50,
            "counts": {"mouse": 0, "scroll": 0, "key": 0, "click": 1},
            "behaviour": {"pathEntropy": 0, "straightness": 1, "mouseTimingCV": 0,
                          "keystrokeCV": 0, "scrolled": False},
            "fingerprint": {"canvas": "err", "webgl_renderer": None, "hardwareConcurrency": 1,
                            "pluginCount": 0, "ua": "Mozilla/5.0 HeadlessChrome", "timezone": "",
                            "notif": "denied", "webdriver": True,
                            "artefacts": ["navigator.webdriver"], "uaCoherent": True},
            "clientTs": 0}


def t_the_collector_separates_an_honest_bot_from_an_honest_human() -> None:
    """The baseline the clone WAS designed to catch: a naive bot that doesn't lie scores near zero,
    a genuine desktop session scores high. If this fails the scorer is broken and every other
    result is meaningless."""
    bot = server.score_bundle(_bot())
    human = server.score_bundle(forged_bundle())   # coherent FP + curved path + realistic timing
    assert bot <= 0.2, f"an honest bot scored {bot}"
    assert human >= 0.7, f"a plausible human scored {human}"
    assert human > bot


def t_the_forged_bundle_defeats_the_collector() -> None:
    """Part 3.4, in code: because the client computes and reports its own signals, a synthetic
    bundle the attacker fully controls is indistinguishable from a real one to the server. This is
    the structural break — and it is the whole lesson of the project, so it must be demonstrated,
    not asserted in prose."""
    assert server.score_bundle(forged_bundle()) >= 0.7


def t_automation_flags_are_multiplicative_and_dominate() -> None:
    """navigator.webdriver / cdc_ / __playwright are near-certain tells, so they multiply the score
    down rather than nudging it — an otherwise perfect bundle with the flag set still fails."""
    perfect = forged_bundle()
    perfect["fingerprint"]["webdriver"] = True
    assert server.score_bundle(perfect) <= 0.2, "webdriver=true did not collapse the score"


def t_a_token_is_single_use() -> None:
    """Score is sealed at mint; the only replay defence is the nonce. Second use must be refused."""
    payload_token = server.fernet.encrypt(server.json.dumps({
        "v": 1, "k": "clone-site-key-123", "action": "submit_contact", "hostname": "localhost",
        "score": 0.9, "nonce": "unit-test-nonce-1", "iat": int(time.time())}).encode()).decode()
    first = server.verify_token(server.SITE_SECRET, payload_token)
    second = server.verify_token(server.SITE_SECRET, payload_token)
    assert first["success"] and first["score"] == 0.9
    assert not second["success"] and "timeout-or-duplicate" in second["error-codes"]


def t_a_stale_token_is_rejected() -> None:
    old = server.fernet.encrypt(server.json.dumps({
        "v": 1, "k": "clone-site-key-123", "action": "x", "hostname": "localhost",
        "score": 0.9, "nonce": "unit-test-old", "iat": int(time.time()) - server.TOKEN_TTL - 5
    }).encode()).decode()
    r = server.verify_token(server.SITE_SECRET, old)
    assert not r["success"] and "timeout-or-duplicate" in r["error-codes"]


def t_a_forged_or_tampered_token_is_rejected() -> None:
    """The token is Fernet (encrypt+HMAC), so a client cannot mint or edit one without the key —
    this is what stops the client reading or raising its own score."""
    assert not server.verify_token(server.SITE_SECRET, "not-a-real-token")["success"]
    assert "invalid-input-response" in \
        server.verify_token(server.SITE_SECRET, "gAAAAABmangled")["error-codes"]


def t_siteverify_enforces_the_secret() -> None:
    good = server.fernet.encrypt(server.json.dumps({
        "v": 1, "k": "clone-site-key-123", "action": "x", "hostname": "localhost",
        "score": 0.9, "nonce": "unit-test-secret", "iat": int(time.time())}).encode()).decode()
    r = server.verify_token("wrong-secret", good)
    assert not r["success"] and "invalid-input-secret" in r["error-codes"]


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CLONE TESTS PASSED")


def test_clone_suite() -> None:   # pytest entry point
    main()


if __name__ == "__main__":
    main()
