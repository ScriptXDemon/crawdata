# server.py — a teaching clone of reCAPTCHA v3's server side.
# Deps: fastapi, uvicorn, cryptography.  Run: uvicorn server:app --reload
#
# Mirrors the real architecture's two load-bearing properties:
#   1. the score is computed AT MINT and SEALED into an opaque token (siteverify only
#      decrypts + checks freshness + consumes — it never re-scores), and
#   2. the token is single-use within a 120s window.
# Part 2 of the write-up explains why score-at-mint is what makes the nonce+timestamp
# replay defence coherent, and Part 3 attacks this exact design.
import json, time, uuid, os, pathlib
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from cryptography.fernet import Fernet, InvalidToken

HERE = pathlib.Path(__file__).parent
app = FastAPI()

# --- keys (server-side only) ---
FERNET_KEY  = os.environ.get("CLONE_FERNET_KEY") or Fernet.generate_key()  # seals tokens
fernet      = Fernet(FERNET_KEY)
SITE_SECRET = os.environ.get("CLONE_SITE_SECRET", "clone-secret-key")       # siteverify secret
KNOWN_SITEKEYS = {"clone-site-key-123"}                                     # public sitekeys

TOKEN_TTL = 120                    # seconds — the same 2-minute window Google uses
_used_nonces = {}                  # nonce -> expiry epoch (single-use replay defence)

def _gc():
    now = time.time()
    for n, exp in list(_used_nonces.items()):
        if exp < now: _used_nonces.pop(n, None)

# ---------------------------------------------------------------- scoring
def score_bundle(b: dict) -> float:
    """Weighted, transparent risk model. Returns 0.0..1.0 bucketed to 0.1,
       exactly like reCAPTCHA v3's 11-bucket output."""
    fp  = b.get("fingerprint", {}) or {}
    beh = b.get("behaviour", {}) or {}
    c   = b.get("counts", {}) or {}

    # ---- behavioural sub-score in [0,1] ----
    # interaction: more independent human input = more human. Capped so a flood
    # of synthetic mouse events can't run the score away on its own.  Weight 0.30.
    interaction = min(1.0, (c.get("mouse",0)/80.0)*0.5
                         + (0.2 if beh.get("scrolled") else 0.0)
                         + min(1.0, c.get("key",0)/10.0)*0.3)
    # curvature: humans wander (straightness ~0.6-0.95). A dead-straight line
    # (==1) or no movement is robotic. Reward the middle band. Weight 0.20.
    s = beh.get("straightness", 1.0)
    curvature = max(0.0, min(1.0, 1.0 - abs(s - 0.8)/0.8)) if c.get("mouse",0) >= 5 else 0.0
    # jitter: human interval timing is noisy (CV ~0.3-1.2); a metronome (CV~0)
    # screams automation/replay. Weight 0.20.
    jitter = max(0.0, min(1.0, beh.get("mouseTimingCV",0.0)/0.6)) if c.get("mouse",0) >= 5 else 0.0
    # entropy: varied movement directions = human. Already normalised. Weight 0.15.
    entropy = max(0.0, min(1.0, beh.get("pathEntropy",0.0)))
    # time-to-execute: sub-500 ms is superhuman (fired ~instantly). Weight 0.15.
    tte = b.get("timeToExecuteMs", 0.0)
    timing = 0.0 if tte < 500 else (0.5 if tte < 1500 else 1.0)

    behavioural = 0.30*interaction + 0.20*curvature + 0.20*jitter + 0.15*entropy + 0.15*timing

    # ---- environment sub-score in [0,1]: start trusting, dock for anomalies ----
    env = 1.0
    if not fp.get("webgl_renderer"):                                  env -= 0.25  # no GPU string
    if fp.get("canvas") in (None, "err"):                             env -= 0.15  # canvas blocked
    if fp.get("hardwareConcurrency", 0) < 2:                          env -= 0.15  # implausible CPU
    if fp.get("pluginCount", 0) == 0 and "Chrome" in fp.get("ua",""): env -= 0.15  # headless-ish
    if not fp.get("timezone"):                                        env -= 0.10
    if fp.get("notif") == "denied":                                   env -= 0.05
    env = max(0.0, min(1.0, env))

    # ---- combine: behaviour is the bulk of what a *client* can measure ----
    raw = 0.6*behavioural + 0.4*env

    # ---- hard automation penalties (multiplicative — these are near-certain tells) ----
    if fp.get("webdriver"):            raw *= 0.15   # navigator.webdriver === true
    if fp.get("artefacts"):            raw *= 0.10   # cdc_, __playwright, phantom, ...
    if not fp.get("uaCoherent", True): raw *= 0.40   # UA claims OS X, platform says Win32

    return round(max(0.0, min(1.0, raw)), 1)          # bucket to 0.1

# ---------------------------------------------------------------- mint
@app.post("/mint")
async def mint(request: Request):
    b = await request.json()
    if b.get("sitekey") not in KNOWN_SITEKEYS:
        return JSONResponse({"error": "invalid-sitekey"}, status_code=400)
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    hostname = urlparse(origin).hostname or ""
    payload = {
        "v": 1, "k": b["sitekey"], "action": b.get("action", "default"),
        "hostname": hostname,
        "score": score_bundle(b),          # <-- SCORED AT MINT, then sealed
        "nonce": uuid.uuid4().hex,          # single-use
        "iat": int(time.time()),
    }
    token = fernet.encrypt(json.dumps(payload).encode()).decode()  # opaque + authenticated
    return {"token": token}                # execute() gets only the token, never the score

# ---------------------------------------------------------------- verify core
def verify_token(secret, response, remoteip=None):
    errors = []
    if not secret:                errors.append("missing-input-secret")
    elif secret != SITE_SECRET:   errors.append("invalid-input-secret")
    if not response:              errors.append("missing-input-response")
    if errors:
        return {"success": False, "error-codes": errors}
    try:
        payload = json.loads(fernet.decrypt(response.encode()))     # no ttl here; we check age
    except (InvalidToken, ValueError):
        return {"success": False, "error-codes": ["invalid-input-response"]}
    if time.time() - payload["iat"] > TOKEN_TTL:                    # expired
        return {"success": False, "error-codes": ["timeout-or-duplicate"]}
    _gc()
    if payload["nonce"] in _used_nonces:                           # replay
        return {"success": False, "error-codes": ["timeout-or-duplicate"]}
    _used_nonces[payload["nonce"]] = payload["iat"] + TOKEN_TTL    # consume
    ts = datetime.fromtimestamp(payload["iat"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"success": True, "score": payload["score"], "action": payload["action"],
            "challenge_ts": ts, "hostname": payload["hostname"], "error-codes": []}

# ---------------------------------------------------------------- siteverify (Google shape)
@app.post("/siteverify")
async def siteverify(request: Request):
    body = parse_qs((await request.body()).decode())
    qp = request.query_params
    def g(k):
        if k in qp: return qp[k]
        return (body.get(k) or [None])[0]
    return JSONResponse(verify_token(g("secret"), g("response"), g("remoteip")))

# ---------------------------------------------------------------- demo backend
@app.post("/submit")
async def submit(request: Request):
    body = parse_qs((await request.body()).decode())
    token = (body.get("captcha_token") or [None])[0]
    ip = request.client.host if request.client else None
    result = verify_token(SITE_SECRET, token, ip)     # secret lives only here, server-side
    threshold = 0.5                                    # per-action threshold lives with the flow
    allowed = bool(result.get("success") and result.get("score", 0) >= threshold)
    return JSONResponse({"verify": result, "threshold": threshold, "allowed": allowed})

# ---------------------------------------------------------------- static
@app.get("/", response_class=HTMLResponse)
def index(): return (HERE / "index.html").read_text(encoding="utf-8")

@app.get("/score.js")
def scorejs():
    return PlainTextResponse((HERE / "score.js").read_text(encoding="utf-8"),
                             media_type="application/javascript")
