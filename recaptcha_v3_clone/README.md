# reCAPTCHA v3 — architecture, a faithful clone, and its structural break

A security-education artefact: understand how reCAPTCHA v3 is built, rebuild it faithfully, then
defeat your own rebuild — and use that to explain precisely why the attack that breaks the clone
does **not** break the real thing. Everything here targets a self-hosted server on your own machine.

```
recaptcha_v3_clone/
├── score.js       client collector — mirrors grecaptcha.ready()/execute()
├── server.py      FastAPI: /mint (scores + seals), /siteverify (Google shape), /submit (demo backend)
├── index.html     a form protected by the clone
├── forge.py       the bypass — a synthetic human bundle that scores 1.0
├── test_clone.py  assert-based self-check (7 tests, no running server needed)
└── README.md
```

## Run it

```bash
pip install fastapi uvicorn cryptography
python -m uvicorn server:app --port 8777
# open http://localhost:8777/  — move the mouse, type, submit → high score, allowed

python forge.py            # the bypass: mints a 1.0 token with NO browser, then proves replay fails
python test_clone.py       # 7 asserts pinning the scorer + token lifecycle
```

Measured end-to-end on this code:

| input | score | verdict |
|---|---|---|
| honest bot — `webdriver=true`, no mouse, 40 ms to submit | **0.0** | blocked |
| forged human — Bézier path + coherent fingerprint (`forge.py`) | **1.0** | **allowed** |
| same token, submitted twice | — | 2nd → `timeout-or-duplicate` |

## How real reCAPTCHA v3 works (the part the clone imitates)

**Request lifecycle.** `api.js?render=SITEKEY` injects a VM-obfuscated client from `gstatic.com`,
renders an invisible **anchor** iframe served from **`google.com`**, and exposes
`grecaptcha.ready()/execute(sitekey,{action})`. `execute()` fires
`POST /recaptcha/api2/reload?k=SITEKEY` carrying `bg` — a BotGuard payload packing the behavioural +
environmental signals — and gets back the `g-recaptcha-response` token (TTL **120 s**). Your backend
then calls `POST /recaptcha/api/siteverify` with `secret + response (+ remoteip)` and receives
`{success, score, action, challenge_ts, hostname}`.

**The load-bearing fact: the score is sealed at MINT, not computed at verify.** siteverify receives
only `secret + response` — **no behavioural data reaches it**. All the signals went to `google.com`
at `reload` time. So the risk score must be computed on Google's frontend when `bg` lands, and
sealed into the opaque token; siteverify only *decrypts → checks freshness → consumes → reports*.
The clone reproduces this exactly (`server.py:/mint` scores then `fernet.encrypt`s; `/siteverify`
never re-scores). This is not pedantry — it is why "buying a v3 token" gives you a *hoped-for* score
frozen at mint, and why the nonce+timestamp replay design is the coherent one (see below).

**What the client measures.** Behaviour (mouse-path entropy, straightness, timing jitter, keystroke
cadence, time-to-`execute`), environment (canvas/WebGL/audio fingerprint, screen geometry,
`navigator.*`, `webdriver`, automation artefacts, timezone-vs-IP), and — decisively — **identity**:
the recaptcha frame is first-party to `google.com`, so it reads Google's own cookies
(`SID/HSID/SSID/NID/_GRECAPTCHA`) and keys a **cross-site reputation graph** built from that cookie's
history across thousands of sites. The ETH Zurich group (Plesner, Vontobel & Wattenhofer, *Breaking
reCAPTCHAv2*, COMPSAC 2024) found reCAPTCHA is *"heavily based on cookie and browser history data"* —
stated about v2, but the mechanism is identical in v3.

**Scoring.** 0.0–1.0 in 0.1 steps. Fresh incognito ≈ 0.3–0.5; logged-in Google user with history ≈
0.7–0.9; headless on a datacenter IP ≈ 0.1. Default threshold 0.5; `action` lets you set per-flow
thresholds and is sealed in the token (verify it matches).

**Obfuscation.** The client is a bytecode VM (BotGuard), not just minified JS — the logic is *data*,
rotated frequently, tamper-evident via the `vh` hash. You cannot statically enumerate the signals or
reproduce the packing; this is why solver services drive *real browsers* instead of reimplementing.

## Why the clone can be broken and real v3 (mostly) cannot

`forge.py` mints a **1.0** with no browser at all. It doesn't crack anything — it hands `/mint` a
bundle claiming ideal behaviour and a clean fingerprint. That is the whole point, stated as a
theorem:

> **The client is the adversary.** The software doing the measuring runs on the attacker's machine.
> Every value it reports is not a *measurement* but an *assertion by the adversary about the
> adversary.*

Encrypting the bundle (the clone uses Fernet) buys confidentiality of the score and integrity *in
transit* — it does nothing for the **truthfulness of the plaintext at creation**, because the client
is the origin of that plaintext. **Encrypting a lie yields an authentic, tamper-proof lie.** So a
pure client-side collector can only catch attackers who don't bother to lie well: `webdriver=true`
catches the lazy, a `defineProperty` defeats it; mouse entropy catches the naive, a Bézier generator
defeats it.

**What real v3 does that the clone structurally cannot** is move the decisive signal *off the
client*: the Google cross-site identity graph, keyed to a cookie whose reputation is asserted by
Google's servers from history across the whole web. You cannot forge it the way you forge a mouse
path, because you don't own the pen. So:

> v3's strength is **not** its client-side measurement — that is forgeable and Google knows it. Its
> strength is Google's cross-site identity graph. The behavioural VM is a speed bump and a telemetry
> funnel; the cookie is the lock. This clone builds a nice speed bump; it cannot build the lock,
> because the lock is *"being Google, watching the whole web"* — which is also exactly why v3 is a
> privacy problem: "can't be forged" and "tracks you across the entire web" are the same property.

### Which attacks transfer to real v3

| attack (breaks the clone) | transfers to real v3? | why |
|---|---|---|
| direct-POST forged bundle | **no** | v3 has no "assert your own features" endpoint; the token is minted server-side from `bg` |
| synthetic mouse path | marginally | behaviour is a minor weight; identity dominates |
| token replay | no — *unless the integrator forgets single-use across their fleet* | tokens are single-use + 120 s; replay is a backend bug, and a common one to audit |
| environment spoofing / stealth | partially | removes automation *penalties* (necessary to not score ~0.1) but adds no identity — necessary, not sufficient |
| structural "the client can lie" | **no** | the decisive signal lives off the client; the lie has nowhere to land |

**Why a solver sells a hoped-for score, not a solve.** There is nothing to solve — a "solution" is
just a token Google already scored highly. Because the score is sealed at mint, by the time a solver
hands you a token the verdict is frozen. Solvers can't compute a good score, only **farm** one: run
real browsers on residential IPs with aged, cookie-rich Google profiles, call the real `execute()`,
and resell whatever number Google's identity graph blessed *that browser* with. You buy someone
else's reputation sight unseen — which is why v3 solvers advertise *probabilistic* scores, why
high-threshold actions resist them, and why the defender's countermeasure is simply to **raise the
threshold** on sensitive flows.

## Relation to the crawler

Our crawler (`cralwer/crawler/captcha.py`) classifies reCAPTCHA v3 as `not_injectable` and refuses to
spend a solver call on it. This clone is *why* that classification is correct: v3 has no DOM sink to
inject a token into, and even a purchased token carries a score decided by an identity graph the
crawler's egress does not have. The honest engineering move — declining v3 rather than pretending to
solve it — falls directly out of the architecture above.

## Sources
- reCAPTCHA v3 & verify docs — developers.google.com/recaptcha/docs/v3, /docs/verify
- Plesner, Vontobel & Wattenhofer, *Breaking reCAPTCHAv2*, COMPSAC 2024 (ETH Zurich) — arxiv.org/abs/2409.08831
- Searles et al., *An Empirical Study & Evaluation of Modern CAPTCHAs*, USENIX Security 2023
- BotGuard VM reverse-engineering — github.com/dsekz/botguard-reverse
