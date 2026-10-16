"""
Pull Kalshi, PredictIt and Polymarket prices for the Fair Model vs. Markets page.

Writes:
  prices.json    - latest prices, read by index.html
  history.jsonl  - one line per run with the headline numbers
  history.json   - compact series the page uses for its over-time charts

Usage:
  python fetch_prices.py              # run once
  python fetch_prices.py --loop 60    # run every 60 seconds (e.g. on election night)

Standard library only (Python 3.9+). On a Mac, run `pip3 install certifi` once if you see
CERTIFICATE_VERIFY_FAILED errors.
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "prices.json")
HISTORY = os.path.join(HERE, "history.jsonl")
HISTORY_JSON = os.path.join(HERE, "history.json")  # compact copy the page reads for the over-time chart

KALSHI = "https://api.elections.kalshi.com/trade-api/v2/events/"
POLY = "https://gamma-api.polymarket.com/events?slug="
PREDICTIT_ALL = "https://www.predictit.org/api/marketdata/all/"

# --- Fair's 14 competitive Senate races ------------------------------------
# (state, seat held now, Kalshi event, Polymarket event slug, Polymarket names to count as Democratic)
# Fair counts independents with the Democrats.
STATES = [
    ("Georgia",        "D", "SENATEGA-26",  "georgia-senate-election-winner",        []),
    ("North Carolina", "R", "SENATENC-26",  "north-carolina-senate-election-winner", []),
    ("New Hampshire",  "D", "SENATENH-26",  "new-hampshire-senate-election-winner",  []),
    ("Minnesota",      "D", "SENATEMN-26",  "minnesota-senate-election-winner",      []),
    ("Alaska",         "R", "SENATEAK-26",  "alaska-senate-election-winner",         ["Mary Peltola"]),
    ("Michigan",       "D", "SENATEMI-26",  "michigan-senate-election-winner",       []),
    ("Texas",          "R", "SENATETX-26",  "texas-senate-election-winner",          []),
    ("Ohio",           "R", "SENATEOHS-26", "ohio-senate-election-winner",           []),
    ("Maine",          "R", "SENATEME-26",  "maine-senate-election-winner",          []),
    ("Iowa",           "R", "SENATEIA-26",  "iowa-senate-election-winner",           []),
    ("Kansas",         "R", "SENATEKS-26",  "kansas-senate-election-winner",         []),
    ("Nebraska",       "R", "SENATENE-26",  "nebraska-senate-election-winner",       []),
    ("South Carolina", "R", "SENATESC-26",  "south-carolina-senate-election-winner", []),
    ("Florida",        "R", "SENATEFLS-26", "florida-senate-election-winner",        []),
]
SENATE_NOW = {"dem": 47, "rep": 53, "control": 51}

KALSHI_SENATE, KALSHI_HOUSE = "CONTROLS-2026", "CONTROLH-2026"
POLY_SENATE, POLY_HOUSE = "which-party-will-win-the-senate-in-2026", "which-party-will-win-the-house-in-2026"

# House popular vote margin
KALSHI_POPVOTE = "KXHOUSEPOPVOTEMARGIN-27NOV03"
KALSHI_BUCKETS = {  # ticker suffix -> Democratic margin range [low, high)
    "B50": (-100, 0), "B1": (0, 2), "B3": (2, 4), "B5": (4, 6), "B7": (6, 8),
    "B9": (8, 10), "B11": (10, 12), "B13": (12, 14), "B15": (14, 16), "B58": (16, 100),
}
POLY_POPVOTE = "2026-midterms-house-popular-vote-margin-of-victory-224"


# --- helpers ----------------------------------------------------------------

def _ssl_context():
    # Python from python.org on macOS ships without system certificates; use certifi's bundle when installed.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fair-model-dashboard/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25, context=SSL_CONTEXT) as r:
        return json.loads(r.read().decode("utf-8"))


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v / 100 if v > 1 else v


def mid(bid, ask, last=None, max_spread=0.10):
    if bid is not None and ask is not None and 0 < ask and 0 <= ask - bid <= max_spread:
        return (bid + ask) / 2
    return last


def dem_share(pairs):
    """pairs: [(is_dem_side, price)] across one race; returns Democratic share scaled to 100%."""
    pairs = [(d, p) for d, p in pairs if p is not None]
    total = sum(p for _, p in pairs)
    if total <= 0:
        raise ValueError("no prices")
    return round(sum(p for d, p in pairs if d) / total, 4)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Kalshi -----------------------------------------------------------------

def kalshi_markets(j):
    ev = j.get("event") or {}
    return ev.get("markets") or j.get("markets") or []


def kalshi_price(m):
    return mid(num(m.get("yes_bid_dollars", m.get("yes_bid"))), num(m.get("yes_ask_dollars", m.get("yes_ask"))),
               num(m.get("last_price_dollars", m.get("last_price"))))


def kalshi_race(j):
    # ticker ends -R for the Republican; everything else (-D, independents) counts as Democratic side
    return dem_share([(not m["ticker"].endswith("-R"), kalshi_price(m)) for m in kalshi_markets(j)])


def kalshi_popvote(j):
    out = []
    for m in kalshi_markets(j):
        key = m["ticker"].split("-")[-1]
        p = kalshi_price(m)
        if key in KALSHI_BUCKETS and p is not None:
            lo, hi = KALSHI_BUCKETS[key]
            out.append({"lo": lo, "hi": hi, "p": p,
                        "bid": num(m.get("yes_bid_dollars", m.get("yes_bid"))), "ask": num(m.get("yes_ask_dollars", m.get("yes_ask")))})
    if len(out) != len(KALSHI_BUCKETS):
        raise ValueError(f"expected {len(KALSHI_BUCKETS)} ranges, got {len(out)}")
    return normalize(out)


# --- Polymarket -------------------------------------------------------------

def poly_markets(resp):
    ev = resp[0] if isinstance(resp, list) and resp else resp
    if not ev:
        raise ValueError("event not found")
    return [m for m in ev.get("markets") or [] if m.get("active", True) and not m.get("closed") and not m.get("archived")]


def poly_price(m):
    try:
        last = float(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        last = None
    return mid(num(m.get("bestBid")), num(m.get("bestAsk")), last)


def poly_race(resp, dem_names):
    def is_dem(title):
        t = (title or "").strip()
        return t.endswith("(D)") or t.endswith("(I)") or t in ("Democrat", "Independent") or t in dem_names
    return dem_share([(is_dem(m.get("groupItemTitle")), poly_price(m)) for m in poly_markets(resp)])


def poly_control(resp):
    pairs = [("Democratic" in (m.get("groupItemTitle") or ""), poly_price(m)) for m in poly_markets(resp)
             if any(p in (m.get("groupItemTitle") or "") for p in ("Democratic", "Republican"))]
    if len(pairs) != 2:
        raise ValueError("expected Democratic and Republican markets")
    return dem_share(pairs)


BRACKET = re.compile(r"^(Democrats|Republicans)\s+(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?%(\+)?$")


def poly_popvote(resp):
    out = []
    for m in poly_markets(resp):
        g = BRACKET.match((m.get("groupItemTitle") or "").strip())
        if not g:
            continue  # skips "Other"
        party, a, b, plus = g.group(1), float(g.group(2)), g.group(3), g.group(4)
        lo, hi = (a, 100.0) if plus else (a, float(b))
        if party == "Republicans":
            lo, hi = -hi + 0.0, -lo + 0.0
        p = poly_price(m)
        if p is not None:
            out.append({"lo": lo, "hi": hi, "p": p, "bid": num(m.get("bestBid")), "ask": num(m.get("bestAsk"))})
    if len(out) < 10:
        raise ValueError(f"only {len(out)} ranges parsed")
    return normalize(out)


def normalize(buckets):
    """Scale midpoints to sum to 1. Bid and ask are scaled by the same factor, for the spread band on the chart."""
    total = sum(b["p"] for b in buckets)
    out = []
    for b in sorted(buckets, key=lambda b: b["lo"]):
        row = {"lo": b["lo"], "hi": b["hi"], "p": round(b["p"] / total, 5)}
        bid, ask = b.get("bid"), b.get("ask")
        row["bid"] = round((bid if bid is not None else b["p"]) / total, 5)
        row["ask"] = round((ask if ask is not None and ask > 0 else b["p"]) / total, 5)
        out.append(row)
    return out


# --- PredictIt --------------------------------------------------------------
# PredictIt publishes every market in one file. Races are found by name, e.g.
# "Which party will win the 2026 US Senate election in Georgia?"

def predictit_find(all_json, pattern):
    rx = re.compile(pattern, re.I)
    for m in all_json.get("markets", []):
        name = m.get("name", "")
        if rx.search(name) and not re.search(r"nominat|primary|runoff", name, re.I):
            return m
    return None


def predictit_price(c):
    return mid(num(c.get("bestSellYesCost")), num(c.get("bestBuyYesCost")), num(c.get("lastTradePrice")))


def predictit_race(market):
    def is_dem(name):
        n = (name or "").lower()
        return n.startswith("dem") or n.startswith("independent")
    return dem_share([(is_dem(c.get("name")), predictit_price(c)) for c in market.get("contracts", [])])


def predictit_control(market):
    return dem_share([(c.get("name", "").lower().startswith("dem"), predictit_price(c))
                      for c in market.get("contracts", []) if c.get("name", "").lower()[:3] in ("dem", "rep")])


# --- run --------------------------------------------------------------------

def build(fetch=get_json, previous=None):
    """Pull everything. Any value that fails keeps its last good number; problems are listed in errors."""
    prev = previous or {}
    errors = []

    def attempt(label, fn, fallback):
        try:
            return fn()
        except Exception as e:
            errors.append(f"{label}: {e}")
            return fallback

    pget = lambda *path: _dig(prev, path)

    # PredictIt: one download covers everything
    pi_all = attempt("predictit download", lambda: fetch(PREDICTIT_ALL), None)

    def pi_market(pattern, label):
        if pi_all is None:
            raise ValueError("download failed")
        m = predictit_find(pi_all, pattern)
        if m is None:
            raise ValueError(f"no market found for {label}")
        return m

    states = []
    for name, held, kev, pslug, dem_names in STATES:
        row = {"name": name, "held": held}
        row["kalshi"] = attempt(f"kalshi {name}", lambda: kalshi_race(fetch(KALSHI + kev + "?with_nested_markets=true")), _prev_state(prev, name, "kalshi"))
        row["predictit"] = attempt(f"predictit {name}", lambda: predictit_race(pi_market(rf"2026.*Senate.*\b{name}\b", name)), _prev_state(prev, name, "predictit"))
        row["polymarket"] = attempt(f"polymarket {name}", lambda: poly_race(fetch(POLY + pslug), dem_names), _prev_state(prev, name, "polymarket"))
        states.append(row)

    data = {
        "updated": now(),
        "senate": {
            "now": SENATE_NOW,
            "states": states,
            "control": {
                "kalshi": attempt("kalshi senate control", lambda: kalshi_race(fetch(KALSHI + KALSHI_SENATE + "?with_nested_markets=true")), pget("senate", "control", "kalshi")),
                "predictit": attempt("predictit senate control", lambda: predictit_control(pi_market(r"control the Senate.*2026", "Senate control")), pget("senate", "control", "predictit")),
                "polymarket": attempt("polymarket senate control", lambda: poly_control(fetch(POLY + POLY_SENATE)), pget("senate", "control", "polymarket")),
            },
        },
        "house": {
            "popvote": {
                "kalshi": attempt("kalshi popular vote", lambda: kalshi_popvote(fetch(KALSHI + KALSHI_POPVOTE + "?with_nested_markets=true")), pget("house", "popvote", "kalshi")),
                "polymarket": attempt("polymarket popular vote", lambda: poly_popvote(fetch(POLY + POLY_POPVOTE)), pget("house", "popvote", "polymarket")),
            },
            "control": {
                "kalshi": attempt("kalshi house control", lambda: kalshi_race(fetch(KALSHI + KALSHI_HOUSE + "?with_nested_markets=true")), pget("house", "control", "kalshi")),
                "predictit": attempt("predictit house control", lambda: predictit_control(pi_market(r"win the House.*2026", "House control")), pget("house", "control", "predictit")),
                "polymarket": attempt("polymarket house control", lambda: poly_control(fetch(POLY + POLY_HOUSE)), pget("house", "control", "polymarket")),
            },
        },
        "errors": errors,
    }
    return data


def _dig(d, path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _prev_state(prev, name, market):
    for s in _dig(prev, ("senate", "states")) or []:
        if s.get("name") == name:
            return s.get(market)
    return None


def pivot(states, market, need):
    """Chance in the race Democrats need for control, ranking by this market. Needs every race priced."""
    vals = [s.get(market) for s in states]
    if any(v is None for v in vals):
        return None
    return sorted(vals, reverse=True)[need - 1]


def write(data):
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, OUT)  # the page never reads a half-written file
    s = data["senate"]
    need = SENATE_NOW["control"] - (SENATE_NOW["dem"] - sum(1 for x in s["states"] if x["held"] == "D"))
    pv = data["house"]["popvote"]
    dem_win = lambda bs: round(sum(b["p"] for b in bs if b["lo"] >= 0), 4) if bs else None
    line = {"t": data["updated"],
            "senate_control": s["control"],
            "senate_pivot": {m: pivot(s["states"], m, need) for m in ("kalshi", "predictit", "polymarket")},
            "states": {x["name"]: [x.get("kalshi"), x.get("predictit"), x.get("polymarket")] for x in s["states"]},
            "house_popvote_dem": {m: dem_win(pv.get(m)) for m in ("kalshi", "polymarket")},
            "house_control": data["house"]["control"]}
    with open(HISTORY, "a") as f:
        f.write(json.dumps(line) + "\n")
    update_history_json(data, need)


def _r(x):
    return None if x is None else round(x * 100, 1)


def update_history_json(data, need):
    """Compact series for the page: one point per 15 minutes for the last 48 hours, one per hour before that."""
    s, pv = data["senate"], data["house"]["popvote"]
    point = {
        "t": data["updated"],
        "sc": [_r(s["control"].get(m)) for m in ("kalshi", "predictit", "polymarket")],
        "sp": [_r(pivot(s["states"], m, need)) for m in ("kalshi", "predictit", "polymarket")],
        "hv": [_r(sum(b["p"] for b in pv[m] if b["lo"] >= 0)) if pv.get(m) else None for m in ("kalshi", "polymarket")],
        "st": {x["name"]: [_r(x.get(m)) for m in ("kalshi", "predictit", "polymarket")] for x in s["states"]},
    }
    try:
        with open(HISTORY_JSON) as f:
            pts = json.load(f)
    except (OSError, ValueError):
        pts = []
    t = datetime.fromisoformat(point["t"])
    slot = lambda x: int(datetime.fromisoformat(x).timestamp() // (15 * 60))
    if pts and slot(pts[-1]["t"]) == slot(point["t"]):
        pts[-1] = point          # one point per 15-minute slot (latest wins)
    else:
        pts.append(point)
    thinned, last_hour = [], None
    for p in pts:
        pt = datetime.fromisoformat(p["t"])
        if (t - pt).total_seconds() > 48 * 3600:
            hour = pt.strftime("%Y-%m-%dT%H")
            if hour == last_hour:
                continue
            last_hour = hour
        thinned.append(p)
    tmp = HISTORY_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(thinned, f, separators=(",", ":"))
    os.replace(tmp, HISTORY_JSON)


def run_once():
    previous = None
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                previous = json.load(f)
        except (OSError, ValueError):
            previous = None
    data = build(previous=previous)
    write(data)
    print(f"[{data['updated']}] " + ("ok" if not data["errors"] else "; ".join(data["errors"])), flush=True)
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    args = ap.parse_args()
    if not args.loop:
        d = run_once()
        sys.exit(1 if len(d["errors"]) > 30 else 0)  # fail only if almost nothing could be read
    while True:
        run_once()
        time.sleep(args.loop)
