/* score.js — a teaching clone of the reCAPTCHA v3 client.
   Exposes window.myCaptcha.ready(cb) and .execute(sitekey,{action}) -> Promise<token>.
   It measures behaviour + environment, ships them to /mint, and returns an
   opaque, server-encrypted token. The client cannot read or forge its own score. */
(function () {
  "use strict";
  var MINT = (document.currentScript && document.currentScript.dataset.mint) || "/mint";

  // ---------- signal buffers ----------
  var t0 = performance.now();          // client init time (for time-to-execute)
  var pageLoadTs = Date.now();
  var mouse = [], scrolls = [], keys = [], clicks = [];
  var CAP = 600;
  function push(a, x) { a.push(x); if (a.length > CAP) a.shift(); }

  addEventListener("mousemove", function (e) {
    push(mouse, { x: e.clientX, y: e.clientY, t: performance.now() });
  }, { passive: true });
  addEventListener("scroll", function () {
    push(scrolls, { y: scrollY, t: performance.now() });
  }, { passive: true });
  addEventListener("keydown", function () {
    push(keys, { t: performance.now() });   // timing only — never the key
  }, { passive: true });
  addEventListener("click", function (e) {
    push(clicks, { x: e.clientX, y: e.clientY, t: performance.now() });
  }, { passive: true });

  // ---------- feature extraction ----------
  function mean(a){ return a.length ? a.reduce(function(s,x){return s+x;},0)/a.length : 0; }
  function std(a){ if(a.length<2) return 0; var m=mean(a);
    return Math.sqrt(mean(a.map(function(x){return (x-m)*(x-m);}))); }

  function pathEntropy() {                 // Shannon entropy of movement directions
    if (mouse.length < 3) return 0;
    var bins = new Array(16).fill(0), n = 0;
    for (var i = 1; i < mouse.length; i++) {
      var dx = mouse[i].x - mouse[i-1].x, dy = mouse[i].y - mouse[i-1].y;
      if (!dx && !dy) continue;
      var b = Math.min(15, Math.floor((Math.atan2(dy, dx) + Math.PI) / (2*Math.PI) * 16));
      bins[b]++; n++;
    }
    if (!n) return 0;
    var h = 0;
    for (var k = 0; k < 16; k++) if (bins[k]) { var p = bins[k]/n; h -= p*Math.log2(p); }
    return h / 4;                          // normalise by log2(16)=4 -> [0,1]
  }
  function straightness() {                // net displacement / path length; 1 = robotic line
    if (mouse.length < 3) return 1;
    var L = 0;
    for (var i = 1; i < mouse.length; i++)
      L += Math.hypot(mouse[i].x-mouse[i-1].x, mouse[i].y-mouse[i-1].y);
    var net = Math.hypot(mouse[mouse.length-1].x-mouse[0].x,
                         mouse[mouse.length-1].y-mouse[0].y);
    return L ? net / L : 1;
  }
  function mouseTimingCV() {               // coefficient of variation of intervals; 0 = metronome
    if (mouse.length < 4) return 0;
    var d = [];
    for (var i = 1; i < mouse.length; i++) d.push(mouse[i].t - mouse[i-1].t);
    var m = mean(d); return m ? std(d)/m : 0;
  }
  function keystrokeCV() {
    if (keys.length < 3) return 0;
    var d = [];
    for (var i = 1; i < keys.length; i++) d.push(keys[i].t - keys[i-1].t);
    var m = mean(d); return m ? std(d)/m : 0;
  }

  // ---------- environment fingerprint ----------
  function canvasHash() {
    try {
      var c = document.createElement("canvas"); c.width = 240; c.height = 60;
      var g = c.getContext("2d");
      g.textBaseline = "top"; g.font = "16px Arial";
      g.fillStyle = "#f60"; g.fillRect(1,1,120,20);
      g.fillStyle = "#069"; g.fillText("clone-cq 🔒 8j!", 2, 2);
      g.fillStyle = "rgba(102,204,0,0.7)"; g.fillText("clone-cq 🔒 8j!", 4, 4);
      var s = c.toDataURL(), h = 5381;
      for (var i = 0; i < s.length; i++) h = ((h<<5)+h + s.charCodeAt(i)) >>> 0;
      return h.toString(16);
    } catch (e) { return "err"; }
  }
  function webgl() {
    try {
      var g = document.createElement("canvas").getContext("webgl");
      if (!g) return { vendor:null, renderer:null };
      var d = g.getExtension("WEBGL_debug_renderer_info");
      return { vendor:   d ? g.getParameter(d.UNMASKED_VENDOR_WEBGL)   : g.getParameter(g.VENDOR),
               renderer: d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER) };
    } catch (e) { return { vendor:"err", renderer:"err" }; }
  }
  function artefacts() {
    var h = [];
    if (navigator.webdriver) h.push("navigator.webdriver");
    for (var k in window) if (k.indexOf("cdc_") === 0) h.push("window."+k);
    ["__playwright","__puppeteer","__pw_manual","__selenium_unwrapped",
     "_phantom","callPhantom","__nightmare","domAutomationController"]
      .forEach(function (p) { if (p in window) h.push("window."+p); });
    if (/HeadlessChrome/.test(navigator.userAgent)) h.push("ua.headless");
    return h;
  }
  function uaCoherent() {
    var u = navigator.userAgent, p = navigator.platform || "";
    if (/Windows/.test(u) && !/Win/.test(p)) return false;
    if (/Mac OS X/.test(u) && !/Mac/.test(p)) return false;
    if (/Android/.test(u) && !/Linux|arm|aarch/i.test(p)) return false;
    if (/Linux/.test(u) && !/Linux|arm|aarch/i.test(p) && !/Android/.test(u)) return false;
    return true;
  }
  function fingerprint() {
    var w = webgl();
    return {
      canvas: canvasHash(), webgl_vendor: w.vendor, webgl_renderer: w.renderer,
      screen: { w:screen.width, h:screen.height, aw:screen.availWidth,
                ah:screen.availHeight, cd:screen.colorDepth, dpr:devicePixelRatio||1 },
      win: { iw:innerWidth, ih:innerHeight, ow:outerWidth, oh:outerHeight },
      hardwareConcurrency: navigator.hardwareConcurrency || 0,
      deviceMemory: navigator.deviceMemory || 0,
      timezone: (Intl.DateTimeFormat().resolvedOptions().timeZone) || "",
      tzOffset: new Date().getTimezoneOffset(),
      languages: navigator.languages || [], platform: navigator.platform || "",
      ua: navigator.userAgent,
      pluginCount: (navigator.plugins && navigator.plugins.length) || 0,
      mimeCount: (navigator.mimeTypes && navigator.mimeTypes.length) || 0,
      notif: (window.Notification && Notification.permission) || "unsupported",
      webdriver: !!navigator.webdriver, artefacts: artefacts(), uaCoherent: uaCoherent()
    };
  }

  function collect(action) {
    var now = performance.now();
    return {
      action: action || "default",
      timeToExecuteMs: now - t0,
      dwellMs: Date.now() - pageLoadTs,
      counts: { mouse:mouse.length, scroll:scrolls.length, key:keys.length, click:clicks.length },
      behaviour: {
        pathEntropy: pathEntropy(), straightness: straightness(),
        mouseTimingCV: mouseTimingCV(), keystrokeCV: keystrokeCV(),
        scrolled: scrolls.length > 0
      },
      fingerprint: fingerprint(), clientTs: Date.now()
    };
  }

  // ---------- public API (mirrors grecaptcha) ----------
  var cbs = [], ready = false;
  function fire(){ ready = true; cbs.forEach(function(f){ try{f();}catch(e){} }); cbs = []; }
  if (/complete|interactive/.test(document.readyState)) setTimeout(fire, 0);
  else document.addEventListener("DOMContentLoaded", function(){ setTimeout(fire, 0); });

  window.myCaptcha = {
    ready: function (cb) { ready ? cb() : cbs.push(cb); },
    execute: function (sitekey, opts) {
      var bundle = collect(opts && opts.action);
      bundle.sitekey = sitekey;
      return fetch(MINT, { method:"POST", headers:{"Content-Type":"application/json"},
                           body: JSON.stringify(bundle) })
        .then(function (r) { return r.json(); })
        .then(function (j) { if (!j.token) throw new Error("mint failed"); return j.token; });
    }
  };
})();
