"""CAPTCHA detection, token injection and solve verification for the C3 (CamoFox) tier.

Split from camofox_client so the hard part — deciding WHAT is on the page and whether a solve
actually worked — is pure Python over a dict of DOM evidence, and can be tested without a browser.

Three things measured on live pages drove this design:

1. `https://accounts.hcaptcha.com/demo` was classified `recaptcha_v2` with `sitekey=None`.
   hCaptcha writes BOTH `h-captcha-response` and `g-recaptcha-response` (drop-in compat), so a
   detector that checks reCAPTCHA first always wins on an hCaptcha page. With the wrong type and a
   null sitekey no solver task could be built, so the solve dead-ended before it started.
   => hCaptcha is now tested BEFORE reCAPTCHA, and the order is pinned by a test.

2. `scrapingcourse.com/antibot-challenge` (Cloudflare managed challenge, `Cf-Mitigated: challenge`)
   was detected as NO captcha at all: it has no `.cf-turnstile` and no widget iframe, only a
   `cf-turnstile-response` input and `#challenge-form`. The single most common real-world wall was
   invisible to the crawler.

3. `grecaptcha.setResponse()`, `hcaptcha.setResponse()` and `turnstile.setResponse()` — the three
   APIs the old injector called — do not exist. Measured `typeof` on a live hCaptcha page: all three
   `undefined`, as were the `grecaptcha`/`hcaptcha` globals themselves. Injection is therefore pure
   DOM (write the response field) plus firing the site's own callback.

The honesty rule, which is the whole point of `VERIFY_JS`: writing a token into the response field
proves nothing, because WE wrote it. A solve is only confirmed by state the SITE controls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# ── Solvability classes ───────────────────────────────────────────────────────
# What can actually be done about each wall, as opposed to what we would like to be true.

TOKEN_INJECTABLE = {          # out-of-band solver token + fire the page callback works
    "recaptcha_v2", "recaptcha_v2_invisible", "recaptcha_enterprise", "hcaptcha", "turnstile",
}
WAIT_ONLY = {                 # session-bound: no detached token can satisfy these. Render and wait.
    "cloudflare_managed", "imperva", "kasada", "perimeterx", "akamai",
}
VENDOR_TASK = {               # solvable, but only via a provider-specific task type
    "datadome", "arkose", "geetest", "awswaf", "mtcaptcha", "keycaptcha", "capy",
    "image_text",             # classic distorted text -> ImageToText / OCR task
}
NOT_INJECTABLE = {            # no DOM sink the site reads; needs execute() override pre-navigation
    "recaptcha_v3",
}
SELF_SOLVING = {              # proof-of-work / no human task: just let it finish
    # Anubis is spreading fast across academic and government sites — this crawler's own targets.
    # It is a SHA-256 proof-of-work priced to deter bulk crawling, not a puzzle to defeat: paying
    # the few hundred ms of CPU is the cooperative behaviour and it simply passes.
    "friendly_captcha", "altcha", "anubis",
}


def solvability(kind: str | None) -> str:
    if not kind:
        return "none"
    for name, s in (("token_injectable", TOKEN_INJECTABLE), ("wait_only", WAIT_ONLY),
                    ("vendor_task", VENDOR_TASK), ("not_injectable", NOT_INJECTABLE),
                    ("self_solving", SELF_SOLVING)):
        if kind in s:
            return name
    return "unknown"


@dataclass
class Detection:
    kind: str | None = None            # canonical captcha id, e.g. hcaptcha
    sitekey: str | None = None
    solvability: str = "none"
    blocking: bool = False             # page content is WITHHELD until this clears
    all_kinds: list[str] = field(default_factory=list)   # everything seen, priority order
    evidence: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return self.kind is not None


# ── Evidence gathering (one round trip) ───────────────────────────────────────
# Returns raw booleans + sitekeys. Deliberately does NOT decide anything: classification is Python
# so it can be unit-tested against captured evidence without a live browser.

EVIDENCE_JS = r"""
(() => {
  const out = {};
  const q  = s => { try { return document.querySelector(s); } catch(e) { return null; } };
  const has = s => !!q(s);
  const attr = (s,a) => { const e=q(s); return e && e.getAttribute ? e.getAttribute(a) : null; };
  // Every script/iframe/link URL on the page — vendor detection keys off these.
  let urls = [];
  try {
    urls = [...document.querySelectorAll('script[src],iframe[src],link[href]')]
      .map(e => e.src || e.href).filter(Boolean);
  } catch(e) {}
  const u = sub => urls.some(s => s.indexOf(sub) !== -1);
  const body = () => { try { return document.body ? document.body.innerText : ''; } catch(e) { return ''; } };
  const html = () => { try { return document.documentElement.outerHTML; } catch(e) { return ''; } };

  out.title = (document.title || '').slice(0,200);
  out.url = location.href;
  out.text_head = body().slice(0,400);
  out.html_len = html().length;

  // --- Cloudflare managed challenge (interstitial; NOT an embedded widget) ---
  out.cf_challenge_form  = has('#challenge-form');
  out.cf_challenge_stage = has('#challenge-stage') || has('#challenge-running');
  out.cf_chl_widget      = has('[id^="cf-chl-widget-"]');
  out.cf_orchestrate     = u('/cdn-cgi/challenge-platform/');
  out.cf_response_input  = has('input[name="cf-turnstile-response"]');
  out.cf_just_a_moment   = /just a moment|checking your browser|performing security verification|enable javascript and cookies/i.test(out.title + ' ' + out.text_head);
  // --- Turnstile standalone widget ---
  out.turnstile_widget = has('.cf-turnstile[data-sitekey]') || has('[data-sitekey][class*="turnstile"]');
  out.turnstile_js     = u('challenges.cloudflare.com/turnstile');
  out.sitekey_turnstile = attr('.cf-turnstile[data-sitekey]','data-sitekey')
                       || attr('[class*="turnstile"][data-sitekey]','data-sitekey');

  // --- hCaptcha (checked before reCAPTCHA: it writes g-recaptcha-response too) ---
  out.hcaptcha_widget = has('.h-captcha') || has('[data-hcaptcha-widget-id]');
  out.hcaptcha_resp   = has('textarea[name="h-captcha-response"], textarea[id^="h-captcha-response"]');
  out.hcaptcha_js     = u('hcaptcha.com/1/api') || u('js.hcaptcha.com');
  out.hcaptcha_frame  = u('newassets.hcaptcha.com') || u('hcaptcha.com/captcha');
  out.sitekey_hcaptcha = attr('.h-captcha[data-sitekey]','data-sitekey');
  out.hcaptcha_invisible = (attr('.h-captcha','data-size') === 'invisible');

  // --- reCAPTCHA ---
  out.recaptcha_widget  = has('.g-recaptcha');
  out.recaptcha_resp    = has('textarea[id^="g-recaptcha-response"], textarea[name^="g-recaptcha-response"]');
  out.recaptcha_frame   = u('google.com/recaptcha') || u('recaptcha.net/recaptcha');
  out.recaptcha_js      = u('recaptcha/api.js') || u('recaptcha/releases');
  out.recaptcha_ent_js  = u('recaptcha/enterprise');
  out.recaptcha_cfg     = (typeof window.___grecaptcha_cfg !== 'undefined');
  out.sitekey_recaptcha = attr('.g-recaptcha[data-sitekey]','data-sitekey');
  out.recaptcha_invisible = (attr('.g-recaptcha','data-size') === 'invisible');
  // v3 declares its sitekey in the script URL as ?render=<key> and renders no widget.
  out.recaptcha_v3_render = (() => {
    const s = urls.find(x => x.indexOf('recaptcha') !== -1 && x.indexOf('render=') !== -1);
    if (!s) return null;
    const m = s.match(/[?&]render=([^&]+)/);
    return (m && m[1] !== 'explicit') ? m[1] : null;
  })();

  // --- Other vendors (URL / marker based) ---
  out.datadome  = u('captcha-delivery.com') || u('js.datadome.co') || has('#datadome-captcha');
  out.arkose    = u('funcaptcha.com') || u('arkoselabs.com') || has('#FunCaptcha') || has('[id*="arkose"]');
  out.geetest   = u('geetest.com') || has('.geetest_holder') || has('[class^="geetest_"]');
  out.awswaf    = u('awswaf.com') || has('#awswaf-captcha') || (typeof window.AwsWafIntegration !== 'undefined');
  out.perimeterx = u('perimeterx.net') || u('px-cloud.net') || has('#px-captcha') || /px-captcha/i.test(html().slice(0,20000));
  out.imperva   = u('_Incapsula_Resource') || has('#main-iframe[src*="_Incapsula_Resource"]');
  out.kasada    = u('/149e9513-01fa-4fb0-aad4-566afd725d1b/') || (typeof window.KPSDK !== 'undefined');
  out.akamai    = u('/akam/') || has('#sec-cpt-if') || /_abck|bm_sz/.test(document.cookie || '');
  out.mtcaptcha = u('mtcaptcha.com') || has('#mtcaptcha') || has('.mtcaptcha');
  out.friendly  = u('friendlycaptcha') || has('.frc-captcha');
  out.altcha    = has('altcha-widget') || u('altcha');
  out.keycaptcha = u('keycaptcha.com') || has('#div_for_keycaptcha');
  out.capy      = u('capy.me') || has('#capy');
  // Classic distorted-text image captcha: an image whose URL says captcha, next to a text input.
  out.image_text = (has('img[src*="captcha" i]') || has('img[id*="captcha" i]'))
                   && has('input[type="text"], input:not([type])');

  // Vendor globals + the APIs the OLD injector called (all three are expected to be undefined —
  // this is recorded so a regression that reintroduces them is visible in the evidence).
  out.g_grecaptcha = typeof window.grecaptcha;
  out.g_hcaptcha   = typeof window.hcaptcha;
  out.g_turnstile  = typeof window.turnstile;
  return out;
})()
"""


# Priority order. First match wins for `kind`.
#   - Cloudflare managed first: it is an interstitial covering the whole page, so nothing else on
#     that DOM is meaningful until it clears.
#   - hCaptcha before reCAPTCHA: hCaptcha writes g-recaptcha-response, so the reverse order
#     misclassifies every hCaptcha page (measured on accounts.hcaptcha.com/demo).
#   - reCAPTCHA v3 last of the reCAPTCHAs: a v2 widget plus a v3 script is a v2 page.
_ORDER = [
    "cloudflare_managed", "imperva", "kasada", "perimeterx", "akamai", "datadome",
    "arkose", "geetest", "awswaf", "mtcaptcha", "keycaptcha", "capy",
    "hcaptcha", "turnstile",
    "recaptcha_enterprise", "recaptcha_v2", "recaptcha_v3",
    "friendly_captcha", "altcha", "anubis", "image_text",
]

# Walls that withhold the page content entirely (as opposed to a widget sitting on a form).
_BLOCKING = {"cloudflare_managed", "imperva", "kasada", "perimeterx", "akamai", "datadome", "awswaf"}


def _present(ev: dict, kind: str) -> bool:
    g = ev.get
    if kind == "cloudflare_managed":
        # The interstitial, not an embedded widget. A bare cf-turnstile-response input is NOT
        # enough to call it managed — a standalone widget has one too — so require a challenge-page
        # marker AND the absence of a real `.cf-turnstile[data-sitekey]`. The "just a moment" text
        # counts as a marker: on scrapingcourse.com/antibot-challenge that was the clearest signal.
        # Getting this wrong the other way costs real money: a managed challenge classified as a
        # standalone widget would be sent to a paid solver for a token that cannot work on it.
        struct = (g("cf_challenge_form") or g("cf_challenge_stage") or g("cf_orchestrate")
                  or g("cf_chl_widget") or g("cf_just_a_moment"))
        return bool(struct) and not g("turnstile_widget")
    if kind == "turnstile":
        # Standalone widget only. Managed challenge is handled above and must not land here.
        return bool(g("turnstile_widget") or (g("cf_response_input") and not _present(ev, "cloudflare_managed")))
    if kind == "hcaptcha":
        return bool(g("hcaptcha_widget") or g("hcaptcha_resp") or g("hcaptcha_js") or g("hcaptcha_frame"))
    if kind == "recaptcha_enterprise":
        return bool(g("recaptcha_ent_js"))
    if kind == "recaptcha_v2":
        return bool(g("recaptcha_widget") or g("recaptcha_frame")
                    or (g("recaptcha_resp") and not _present(ev, "hcaptcha")))
    if kind == "recaptcha_v3":
        return bool(g("recaptcha_v3_render"))
    if kind == "image_text":
        return bool(g("image_text"))
    if kind == "friendly_captcha":
        return bool(g("friendly"))
    return bool(g(kind))


def _sitekey_for(ev: dict, kind: str) -> str | None:
    if kind == "hcaptcha":
        return ev.get("sitekey_hcaptcha")
    if kind == "turnstile":
        return ev.get("sitekey_turnstile")
    if kind in ("recaptcha_v2", "recaptcha_enterprise"):
        return ev.get("sitekey_recaptcha")
    if kind == "recaptcha_v3":
        return ev.get("recaptcha_v3_render")
    # Cloudflare picks the managed-challenge sitekey itself and embeds it in an obfuscated
    # orchestrate payload. It is genuinely not extractable from the DOM — returning None here is
    # the honest answer, and is why cloudflare_managed is WAIT_ONLY rather than token_injectable.
    return None


def classify(ev: dict | None) -> Detection:
    """Decide what is on the page from gathered DOM evidence. Pure function — no I/O."""
    if not isinstance(ev, dict) or not ev:
        return Detection()
    kinds = [k for k in _ORDER if _present(ev, k)]
    if not kinds:
        return Detection(evidence=ev)
    kind = kinds[0]
    sub = kind
    if kind == "recaptcha_v2" and ev.get("recaptcha_invisible"):
        sub = "recaptcha_v2_invisible"
    return Detection(kind=sub, sitekey=_sitekey_for(ev, kind), solvability=solvability(sub),
                     blocking=kind in _BLOCKING, all_kinds=kinds, evidence=ev)


# ── Token injection ───────────────────────────────────────────────────────────

def injection_js(kind: str, token: str) -> str:
    """JS that writes a solver token into the page and fires the site's own success callback.

    Pure DOM. The vendor `setResponse()` APIs the previous version called do not exist — measured
    `undefined` on a live page for all three vendors.
    """
    tok = json.dumps(token)
    if kind in ("recaptcha_v2", "recaptcha_v2_invisible", "recaptcha_enterprise"):
        fields = ('textarea[id^="g-recaptcha-response"], textarea[name^="g-recaptcha-response"], '
                  'textarea.g-recaptcha-response')
        walk = r"""
    // reCAPTCHA keeps render() params in ___grecaptcha_cfg.clients. The PUBLIC param names
    // (sitekey, callback, size) survive minification because the vendor reads them by name from
    // the site's object — only the nesting keys around them are minified. So: recurse, find the
    // object owning a `sitekey` key, take its `callback`.
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) {
      const seen = new Set(); const stack = [{o: cfg.clients, p: 'clients', d: 0}];
      while (stack.length) {
        const {o, p, d} = stack.pop();
        if (!o || typeof o !== 'object' || d > 6 || seen.has(o)) continue;
        seen.add(o);
        if ('sitekey' in o) {
          let cb = o.callback;
          if (cb === undefined) cb = o['promise-callback'];
          if (cb === undefined) {
            const hit = Object.entries(o).find(([k, v]) => typeof v === 'function'
              && /callback/i.test(k) && !/(expired|error|chalexpired|close|open)/i.test(k));
            if (hit) cb = hit[1];
          }
          if (typeof cb === 'function') call(cb, p + '.callback');
          else if (typeof cb === 'string' && typeof window[cb] === 'function') call(window[cb], cb);
          out.configs++;
          continue;
        }
        for (const [k, v] of Object.entries(o))
          if (v && typeof v === 'object') stack.push({o: v, p: p + '.' + k, d: d + 1});
      }
    }"""
    elif kind == "hcaptcha":
        # hCaptcha populates BOTH response fields; fill both or a drop-in-compat form reads an
        # empty g-recaptcha-response and rejects the submit.
        fields = ('textarea[name="h-captcha-response"], textarea[id^="h-captcha-response"], '
                  'textarea[name="g-recaptcha-response"], textarea[id^="g-recaptcha-response"]')
        walk = """
    for (const w of document.querySelectorAll('.h-captcha')) {
      try { w.setAttribute('data-hcaptcha-response', TOKEN); } catch (e) {}
    }"""
    elif kind == "turnstile":
        fields = 'input[name="cf-turnstile-response"], input[name="g-recaptcha-response"]'
        walk = ""
    else:
        return ""
    return r"""
(() => {
  const TOKEN = %s;
  const out = {kind: %s, fields: 0, callbacks: [], configs: 0, error: null};
  const done = new Set();
  const call = (fn, label) => {
    if (typeof fn !== 'function' || done.has(fn)) return;   // dedupe: data-callback and the
    done.add(fn);                                            // registry often name the same fn,
    try { fn(TOKEN); out.callbacks.push(label); }            // and a double call = double submit
    catch (e) { out.callbacks.push(label + ' THREW:' + e); }
  };
  try {
    for (const f of document.querySelectorAll(%s)) {
      f.value = TOKEN;
      if (f.tagName === 'TEXTAREA') f.innerHTML = TOKEN;   // some handlers read textContent
      out.fields++;
      // Harmless for the vendors (they bind no listener on their own output field) but it is what
      // unlocks a bespoke form that enables its submit button on change.
      try { f.dispatchEvent(new Event('input', {bubbles: true})); } catch (e) {}
      try { f.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
    }
    for (const el of document.querySelectorAll('[data-callback]')) {
      const n = el.getAttribute('data-callback');
      if (n && typeof window[n] === 'function') call(window[n], 'data-callback:' + n);
    }%s
  } catch (e) { out.error = String(e); }
  return out;
})()
""" % (tok, json.dumps(kind), json.dumps(fields), walk)


# Prefer clicking the real control: form.submit() fires neither the submit event nor validation, so
# it silently skips an onsubmit handler that attaches the token or computes a signature.
SUBMIT_JS = r"""
(() => {
  const out = {submitted: false, method: null, form: false, error: null};
  try {
    const resp = document.querySelector('textarea[id^="g-recaptcha-response"], '
      + 'textarea[name="h-captcha-response"], input[name="cf-turnstile-response"]');
    const form = (resp && resp.closest('form')) || document.querySelector('#challenge-form, form');
    out.form = !!form;
    if (!form) return out;
    const btn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])')
             || form.querySelector('button, input[type="button"]');
    if (btn) { btn.click(); out.submitted = true; out.method = 'click'; return out; }
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit(); out.submitted = true; out.method = 'requestSubmit'; return out;
    }
    form.submit(); out.submitted = true; out.method = 'submit(bypasses onsubmit)';
  } catch (e) { out.error = String(e); }
  return out;
})()
"""


# ── Verification ──────────────────────────────────────────────────────────────
# Decides on state the SITE controls. A filled response field proves nothing: we filled it.

VERIFY_JS = r"""
(() => {
  const out = {solved: false, reasons: [], token: {}, error: null};
  try {
    const val = s => { const e = document.querySelector(s); return e ? String(e.value || '') : null; };
    for (const [k, s] of [['recaptcha', 'textarea[id^="g-recaptcha-response"]'],
                          ['hcaptcha',  'textarea[name="h-captcha-response"]'],
                          ['turnstile', 'input[name="cf-turnstile-response"]']]) {
      const v = val(s);
      if (v !== null) out.token[k] = {len: v.length, head: v.slice(0, 4)};
    }
    const title = document.title || '';
    const body = document.body ? document.body.innerText : '';
    out.url = location.href;
    out.interstitial = /just a moment|checking your browser|performing security verification/i.test(title + ' ' + body.slice(0, 400));
    out.challenge_form = !!document.getElementById('challenge-form');
    out.success_text = /challenge\s*success|verification success|verified successfully|access granted/i.test(body);
    out.widget_gone = !document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"]');

    if (out.success_text) { out.solved = true; out.reasons.push('success-text'); }
    if (!out.interstitial && !out.challenge_form) {
      out.solved = true; out.reasons.push('no-interstitial');
    }
    // The classic false positive: our own write, nothing else moved. Name it and refuse it.
    const wrote = Object.values(out.token).some(t => t && t.len > 20);
    if (wrote && !out.solved) out.reasons.push('TOKEN-PRESENT-BUT-UNVERIFIED');
  } catch (e) { out.error = String(e); }
  return out;
})()
"""


def verified(v: dict | None) -> bool:
    """True only when the SITE showed change. Never trusts a token we wrote ourselves."""
    if not isinstance(v, dict):
        return False
    if v.get("interstitial") or v.get("challenge_form"):
        return False           # still walled, whatever else the page says
    return bool(v.get("solved"))


# ── Fingerprint coherence ─────────────────────────────────────────────────────
# Why this lives in the captcha module: an incoherent fingerprint is the reason a captcha cannot be
# solved, so it belongs with the diagnosis rather than three layers away.
#
# Measured inside the running CamoFox container:
#     navigator.userAgent  = "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) ... Firefox/135.0"
#     navigator.platform   = "Linux armv81"          <- ARM, on an x86_64 host, claiming x86_64
#
# Real Firefox on x86_64 Linux reports platform "Linux x86_64". Camoufox samples its platform string
# from a device corpus that includes ARM handsets without constraining it to match the UA it built.
# On scrapingcourse.com/antibot-challenge the consequence is exact and repeatable: Cloudflare mounts
# the widget host, then never injects the challenge iframe. The slot holds only the hidden response
# input, the accessibility tree contains no checkbox, and the page sits unchanged for 75s+. It is not
# that the challenge was failed — it was never offered.
#
# This cannot be fixed from here (the value comes from the vendored browser, which exposes no launch
# hook), so the job of this code is to NAME it. An unsolvable captcha with a known cause is a bug
# report; an unsolvable captcha with no cause is a mystery that gets retried forever.

FINGERPRINT_JS = r"""
({ua: navigator.userAgent, platform: navigator.platform,
  cores: navigator.hardwareConcurrency, langs: (navigator.languages||[]).join(','),
  webdriver: navigator.webdriver, plugins: (navigator.plugins||[]).length,
  screen: [screen.width, screen.height].join('x'),
  outer: [window.outerWidth, window.outerHeight].join('x')})
"""

# UA architecture token -> the platform strings a real browser would report with it.
_UA_ARCH = (
    ("x86_64", ("Linux x86_64", "Win32", "MacIntel")),
    ("Win64",  ("Win32",)),
    ("aarch64", ("Linux aarch64", "Linux armv8l", "Linux armv81", "MacIntel")),
    ("armv",   ("Linux armv7l", "Linux armv8l", "Linux armv81")),
)


def fingerprint_flaws(fp: dict | None) -> list[str]:
    """Contradictions an anti-bot check can spot in one line of JS. Empty list = coherent.

    Only reports things that are provably inconsistent WITH EACH OTHER, never 'this looks unusual' —
    an unusual-but-consistent fingerprint is just a rare machine, and flagging those would bury the
    real defects in noise."""
    if not isinstance(fp, dict):
        return []
    flaws: list[str] = []
    ua, plat = str(fp.get("ua") or ""), str(fp.get("platform") or "")
    if ua and plat:
        for token, allowed in _UA_ARCH:
            if token in ua:
                if plat not in allowed:
                    flaws.append(f"userAgent claims {token} but navigator.platform is {plat!r} "
                                 f"(a real browser would report one of {allowed})")
                break
    if fp.get("webdriver"):
        flaws.append("navigator.webdriver is true — the automation flag is not masked")
    return flaws


# ── HTTP-level detection (no browser needed) ──────────────────────────────────
# Most walls announce themselves in the response headers. Recognising one at C1 costs nothing and
# tells the engine WHICH wall it hit, so it can escalate to C3 or give up deliberately instead of
# retrying a 403 that will never change.
#
# Two rules keep this from crying wolf:
#
#  1. Only a DEFINITIVE marker counts. `cf_clearance` in a Set-Cookie means the site sits behind
#     Cloudflare — which is most of the web — not that we were challenged. `cf-mitigated: challenge`
#     means we were challenged. Conflating them would report a wall on every successful fetch.
#  2. Body matching keys on structural markers only — element ids, script paths — never on the word
#     "cloudflare" or "captcha" in prose. This crawler reads defence and procurement pages that
#     discuss security vendors by name; a substring match would flag those as blocked.

# vendor -> (header rules, cookie-name markers, body markers)
#   header rule: (name, expected substring or None for "present at all")
_HTTP_SIGNATURES: dict[str, tuple[tuple, tuple, tuple]] = {
    "cloudflare_managed": ((("cf-mitigated", "challenge"),), (), ("/cdn-cgi/challenge-platform/", "cf_chl_opt", "cf-browser-verification")),
    "datadome":  ((("x-datadome-cid", None), ("x-dd-b", None)), ("datadome=",), ("ct.captcha-delivery.com", "geo.captcha-delivery.com")),
    "perimeterx": ((("x-px-authorization", None),), ("_px3=", "_pxhd="), ("window._pxAppId", "_pxAction")),
    "akamai":    ((("x-akamai-session-info", None),), ("_abck=",), ("bmak.", "#sec-cpt-if")),
    "imperva":   ((("x-iinfo", None), ("x-cdn", "Incapsula")), ("incap_ses_", "visid_incap_", "reese84="), ("_Incapsula_Resource",)),
    "kasada":    ((("x-kpsdk-ct", None), ("x-kpsdk-cd", None)), (), ("149e9513-01fa-4fb0-aad4-566afd725d1b",)),
    "awswaf":    ((("x-amzn-waf-action", None),), ("aws-waf-token=",), ("awswaf.com", "/awswaf/")),
    "anubis":    ((), ("techaro.lol-anubis-auth",), ('id="anubis_challenge"', "/.within.website/x/cmd/anubis/")),
    # Widget types key on the WIDGET, never on the vendor script URL. Measured on
    # scrapingcourse.com/table-parsing: a clean 200 that loads the turnstile script site-wide was
    # reported as a Turnstile wall, which would have written a false captcha_type onto a page that
    # fetched perfectly. Sites load these scripts globally and mount the widget on one page.
    "turnstile": ((), (), ("cf-turnstile-response", 'class="cf-turnstile"', "cf-turnstile ")),
    "hcaptcha":  ((), (), ("h-captcha-response", 'class="h-captcha"', "h-captcha ")),
    "recaptcha_v2": ((), (), ("g-recaptcha-response", 'class="g-recaptcha"', "g-recaptcha ")),
    "arkose":    ((), (), ("client-api.arkoselabs.com", "cdn.funcaptcha.com/fc")),
    "geetest":   ((), (), ("gcaptcha4.geetest.com", "static.geetest.com")),
}

# A wall only withholds content on these. On a 200 the same marker usually just means the widget is
# embedded in a page we successfully read — worth recording, not worth calling a block.
_BLOCK_STATUSES = {401, 403, 405, 406, 429, 456, 503, 999}


def detect_http(status: int | None, headers: dict | None = None,
                body: str | None = None) -> Detection:
    """Identify an anti-bot wall from the HTTP response alone. No browser, no cost.

    `blocking` is True only when the response actually withheld content, so a page that merely
    embeds a captcha widget is reported without being treated as a failure."""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    cookie_blob = " ".join(v for k, v in h.items() if k in ("set-cookie", "cookie"))
    snippet = (body or "")[:60000]
    hits: list[str] = []

    for vendor, (hdr_rules, cookies, body_marks) in _HTTP_SIGNATURES.items():
        if any((name in h) and (val is None or val.lower() in h[name].lower())
               for name, val in hdr_rules):
            hits.append(vendor)
            continue
        if any(cm in cookie_blob for cm in cookies):
            hits.append(vendor)
            continue
        if any(bm in snippet for bm in body_marks):
            hits.append(vendor)

    # LinkedIn's bespoke refusal. A status nothing else uses, so it needs no marker.
    if status == 999 and "linkedin" not in hits:
        hits.append("perimeterx")

    # A Cloudflare interstitial is small, refused, and says so in the title.
    if (status in (403, 503) and "cloudflare_managed" not in hits
            and "<title>just a moment" in snippet.lower()):
        hits.append("cloudflare_managed")

    if not hits:
        return Detection()
    ordered = [k for k in _ORDER if k in hits] or hits
    kind = ordered[0]
    return Detection(kind=kind, solvability=solvability(kind),
                     blocking=(status in _BLOCK_STATUSES) and kind in _BLOCKING,
                     all_kinds=ordered,
                     evidence={"status": status, "source": "http"})
