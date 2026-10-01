#!/usr/bin/env python3
"""
Apple Store pickup checker for iPhone 18 Pro / Pro Max -> ntfy alerts.

Modes:
  watch    alert only on variants that are NEWLY in stock since the last run
  summary  send a list of everything currently in stock (hourly digest)
  test     send a test notification and print what's in stock

Env:
  NTFY_TOPIC   (required) your ntfy topic name
  NTFY_SERVER  (optional) default https://ntfy.sh
  NTFY_TOKEN   (optional) access token if your topic is protected
  STATE_FILE   (optional) default state.json
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- catalog (from apple.com, Sept 2026; US parts are the same for every carrier) ----
MODELS = {
    "promax": ("iPhone 18 Pro Max", "6.9-inch-display"),
    "pro": ("iPhone 18 Pro", "6.3-inch-display"),
}
STORAGE_LABEL = {"256gb": "256GB", "512gb": "512GB", "1tb": "1TB", "2tb": "2TB"}
COLOR_LABEL = {"black": "Black", "silver": "Silver", "glacier": "Glacier", "burgundy": "Burgundy"}
PARTS = {
    "promax": {
        "256gb": {"black": "MJW44LL/A", "silver": "MJW54LL/A", "burgundy": "MJW64LL/A", "glacier": "MJW74LL/A"},
        "512gb": {"black": "MJW84LL/A", "silver": "MJW94LL/A", "burgundy": "MJWA4LL/A", "glacier": "MJWC4LL/A"},
        "1tb":   {"black": "MJWD4LL/A", "silver": "MJWE4LL/A", "burgundy": "MJWF4LL/A", "glacier": "MJWG4LL/A"},
        "2tb":   {"black": "MJWH4LL/A", "silver": "MJWJ4LL/A", "burgundy": "MJWK4LL/A", "glacier": "MJWL4LL/A"},
    },
    "pro": {
        "256gb": {"black": "MJQ34LL/A", "silver": "MJQ44LL/A", "burgundy": "MJQ54LL/A", "glacier": "MJQ64LL/A"},
        "512gb": {"black": "MJQ74LL/A", "silver": "MJQ84LL/A", "burgundy": "MJQ94LL/A", "glacier": "MJQA4LL/A"},
        "1tb":   {"black": "MJQC4LL/A", "silver": "MJQD4LL/A", "burgundy": "MJQE4LL/A", "glacier": "MJQF4LL/A"},
        "2tb":   {"black": "MJQG4LL/A", "silver": "MJQH4LL/A", "burgundy": "MJQJ4LL/A", "glacier": "MJQK4LL/A"},
    },
}
INFO = {pn: (m, s, c) for m, ss in PARTS.items() for s, cs in ss.items() for c, pn in cs.items()}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
BLOCKED_ALERT_EVERY = 6 * 3600  # don't spam "Apple blocked us" more than every 6h


def label(pn, short=False):
    m, s, c = INFO[pn]
    name = ("Pro Max" if m == "promax" else "Pro") if short else MODELS[m][0]
    return f"{name} {STORAGE_LABEL[s]} {COLOR_LABEL[c]}"


def buy_url(pn, carrier):
    m, s, c = INFO[pn]
    return f"https://www.apple.com/shop/buy-iphone/iphone-18-pro/{MODELS[m][1]}-{s}-{c}-{carrier}"


def tracked_parts(cfg):
    out = []
    exclude = {e.upper() for e in cfg.get("exclude", [])}
    for model, sel in cfg.get("track", {}).items():
        for s in sel.get("storages", []):
            for c in sel.get("colors", []):
                pn = PARTS[model][s.lower()][c.lower()]
                if pn not in exclude and pn.split("LL/A")[0] not in exclude:
                    out.append(pn)
    return out


# ---------------- Apple ----------------
class Blocked(Exception):
    pass


def fetch_chunk(parts, zip_code):
    q = [("fae", "true"), ("pl", "true"), ("mts.0", "regular"), ("mts.1", "compact"),
         ("searchNearby", "true"), ("location", zip_code)]
    q += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    url = "https://www.apple.com/shop/fulfillment-messages?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.apple.com/shop/buy-iphone/iphone-18-pro",
    })
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            data = json.loads(body)
            pm = data["body"]["content"]["pickupMessage"]
            if "stores" not in pm:
                raise RuntimeError(pm.get("errorMessage") or "No stores in response")
            return pm["stores"]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (403, 429, 503, 541):
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except (json.JSONDecodeError, KeyError) as e:
            last = f"Bad response ({e.__class__.__name__})"
            time.sleep(5 * (attempt + 1))
        except urllib.error.URLError as e:
            last = str(e.reason)
            time.sleep(5 * (attempt + 1))
    raise Blocked(last)


def check(cfg, parts):
    stores = {}
    for i in range(0, len(parts), 16):
        for st in fetch_chunk(parts[i:i + 16], cfg["zip"]):
            dist = float(st.get("storedistance") or 9999)
            if dist > float(cfg["radius_miles"]):
                continue
            rec = stores.setdefault(st["storeNumber"], {"name": st["storeName"], "city": st.get("city"), "dist": dist, "avail": {}})
            for pn, v in (st.get("partsAvailability") or {}).items():
                if v.get("pickupDisplay") == "available":
                    rec["avail"][pn] = v.get("pickupSearchQuote") or "Available"
    # part -> [(store, dist, quote)]
    by_part = {}
    for num, st in sorted(stores.items(), key=lambda kv: kv[1]["dist"]):
        for pn, quote in st["avail"].items():
            by_part.setdefault(pn, []).append((st["name"], st["dist"], quote))
    return by_part, sorted((s["name"] for s in stores.values()))


# ---------------- ntfy ----------------
def ntfy(title, message, priority="default", tags="", click=None, actions=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC not set; would have sent:\n", title, "\n", message)
        return
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message, "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}[priority]}
    if tags:
        payload["tags"] = tags.split(",")
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    req = urllib.request.Request(server, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        print("ntfy:", r.status, title)


# ---------------- state ----------------
def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"available": {}, "last_blocked_alert": 0}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "watch")).strip() or "watch"
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "config.json")) as f:
        cfg = json.load(f)
    state_path = os.environ.get("STATE_FILE", os.path.join(here, "state.json"))
    state = load_state(state_path)
    carrier = cfg.get("carrier_for_links", "t-mobile")

    parts = tracked_parts(cfg)
    if not parts:
        print("Nothing selected in config.json -> track")
        return
    print(f"Mode={mode} | {len(parts)} variants | ZIP {cfg['zip']} within {cfg['radius_miles']} mi")

    if mode == "test":
        ntfy("Apple stock checker connected", f"Tracking {len(parts)} iPhone variants near {cfg['zip']}.", tags="white_check_mark")

    try:
        by_part, store_names = check(cfg, parts)
    except Blocked as e:
        print("Apple request failed:", e)
        now = time.time()
        if now - state.get("last_blocked_alert", 0) > BLOCKED_ALERT_EVERY:
            ntfy("Stock checker can't reach Apple", f"apple.com refused the request ({e}). Will keep retrying.", priority="low", tags="warning")
            state["last_blocked_alert"] = now
        save_state(state_path, state)
        sys.exit(1)

    print("Stores in range:", ", ".join(store_names) or "none")
    for pn in parts:
        hits = by_part.get(pn, [])
        print(f"  {label(pn, True):<26} {'; '.join(f'{n} ({q})' for n, _, q in hits) if hits else '-'}")

    prev = state.get("available", {})
    now_avail = {pn: [h[0] for h in hits] for pn, hits in by_part.items()}

    # --- new-stock alerts (every mode) ---
    fresh = {pn: [h for h in hits if h[0] not in prev.get(pn, [])] for pn, hits in by_part.items()}
    fresh = {pn: hits for pn, hits in fresh.items() if hits}
    for pn, hits in fresh.items():
        msg = "\n".join(f"{n} ({d:.0f} mi): {q}" for n, d, q in hits)
        ntfy(f"IN STOCK: {label(pn)}", msg, priority="urgent", tags="iphone,rotating_light",
             click=buy_url(pn, carrier),
             actions=[{"action": "view", "label": "Open Apple Store", "url": buy_url(pn, carrier)}])

    # --- hourly digest ---
    if mode in ("summary", "test"):
        if by_part:
            lines = []
            for pn in parts:
                if pn in by_part:
                    stores = ", ".join(f"{n}" for n, _, _ in by_part[pn])
                    lines.append(f"• {label(pn, True)} — {stores}")
            ntfy(f"{len(by_part)} of {len(parts)} tracked iPhones in stock near {cfg['zip']}",
                 "\n".join(lines), priority="default", tags="iphone,clipboard")
        elif cfg.get("hourly_summary_when_nothing_in_stock") or mode == "test":
            ntfy("No tracked iPhones in stock",
                 f"Checked {len(parts)} variants at {len(store_names)} stores within {cfg['radius_miles']} mi of {cfg['zip']}.",
                 priority="low", tags="hourglass")

    state["available"] = now_avail
    state["last_run"] = int(time.time())
    save_state(state_path, state)


if __name__ == "__main__":
    main()
