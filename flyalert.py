#!/usr/bin/env python3
"""
Flybubble flyability alerts.

Once a day: fetch each site's Flybubble forecast, read the 5-day summary
table's "Flyable" column, and push a digest to your phone via ntfy.sh.
Flybubble "Yes" slots are listed as Flyable and "Marginal" slots as Marginal.
Slots that are new (or have changed status) since the previous run are marked
NEW; a new or upgraded Flyable slot sends the alert at high priority. Slots
that have dropped out are listed too.
"""
import json, os, re, sys, time
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SITES = [
    ("Hale Towans / Black Cliff", "hale-towansblack-cliff-15419"),
    ("Perranporth",               "perranporth-15499"),
    ("Sandy Mouth, Bude",         "sandy-mouth-bude-15411"),
    ("Cornborough",               "cornborough-14661"),
    ("Putsborough",               "putsborough-15505"),
    ("Woolacombe",                "woolacombe-6819"),
    ("Trentishoe Fields",         "trentishoe-fields-15530"),
    ("Countisbury",               "countisbury-15519"),
    ("Bossington Hill",           "bossington-hill-6132"),
]

BASE = "https://weather.flybubble.com/"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
ALWAYS_NOTIFY = os.environ.get("ALWAYS_NOTIFY", "0") == "1"  # send even when nothing is flyable
HEADERS = {"User-Agent": "flyalert/1.0 (personal daily flyability digest; 1 request per site per day)"}

MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
PERIOD_ORDER = {"Morning": 0, "Midday": 1, "Afternoon": 2}
ROW_RE = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s*\|?\s*(Morning|Midday|Afternoon)\s*\|?\s*(Yes|No|Marginal)\b"
    r"(?:\s*\|?\s*(\d+\s*kph\s+[NESW]{1,3}))?", re.I)


LABEL = {"yes": "Flyable", "marginal": "Marginal"}


def status_of(text: str) -> str:
    """Map Flybubble's Flyable cell to 'yes', 'marginal' or 'no'."""
    t = text.strip().lower()
    if t.startswith("yes"):
        return "yes"
    if t.startswith("marginal"):
        return "marginal"
    return "no"


def to_iso(day: str, mon: str) -> str:
    today = date.today()
    m = MONTHS[mon.title()]
    year = today.year + 1 if m < today.month - 6 else today.year  # Dec -> Jan rollover
    return date(year, m, int(day)).isoformat()


def parse(html: str) -> list[dict]:
    """Return [{date, period, status, wind}] from the 5-day summary table."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    # 1) Proper <table> with a "Flyable" header
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        hdr = [c.get_text(" ", strip=True).lower() for c in trs[0].find_all(["th", "td"])]
        if "flyable" not in hdr:
            continue
        idx = {h: i for i, h in enumerate(hdr)}
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < len(hdr):
                continue
            m = re.match(r"\w{3}\s+(\d{1,2})\s+(\w{3})", cells[idx.get("date", 0)])
            if not m:
                continue
            rows.append({
                "date": to_iso(m.group(1), m.group(2)),
                "period": cells[idx.get("period", 1)].title(),
                "status": status_of(cells[idx["flyable"]]),
                "wind": cells[idx["launch wind"]] if "launch wind" in idx else "",
            })
        if rows:
            return rows

    # 2) Fallback: regex over the page text
    text = soup.get_text(" ", strip=True)
    for m in ROW_RE.finditer(text):
        rows.append({
            "date": to_iso(m.group(2), m.group(3)),
            "period": m.group(4).title(),
            "status": status_of(m.group(5)),
            "wind": (m.group(6) or "").strip(),
        })
    return rows


def fetch(slug: str) -> list[dict]:
    r = requests.get(BASE + slug, headers=HEADERS, timeout=30)
    r.raise_for_status()
    rows = parse(r.text)
    if not rows:
        raise ValueError("forecast table not found (page layout may have changed)")
    return rows


def key(slug, row):
    return f"{slug}|{row['date']}|{row['period']}"


def build_message(flyable, new_keys, dropped, errors, names):
    lines = []
    by_day = {}
    for k, info in flyable.items():
        slug, d, p = k.split("|")
        by_day.setdefault(d, []).append((PERIOD_ORDER.get(p, 9), names[slug], p, info, k in new_keys))
    for d in sorted(by_day):
        lines.append(datetime.fromisoformat(d).strftime("%a %d %b"))
        for _, name, p, info, is_new in sorted(by_day[d], key=lambda x: (x[1], x[0])):
            wind = info.get("wind", "")
            label = LABEL[info.get("status", "yes")]
            lines.append(f"  • {name}: {p} {label}{' ' + wind if wind else ''}{'  ✨NEW' if is_new else ''}")
    if dropped:
        lines.append("\nNo longer flyable or marginal:")
        for k in sorted(dropped, key=lambda k: k.split("|")[1:]):
            slug, d, p = k.split("|")
            lines.append(f"  • {names[slug]}: {datetime.fromisoformat(d).strftime('%a %d')} {p}")
    if errors:
        lines.append("\n⚠️ Couldn't read: " + ", ".join(errors))
    if not flyable:
        lines.insert(0, "Nothing flyable or marginal in the forecast window.")
    return "\n".join(lines)


def notify(title, body, high):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; message follows:\n" + title + "\n" + body)
        return
    requests.post(NTFY_SERVER, json={
        "topic": NTFY_TOPIC, "title": title, "message": body,
        "priority": 4 if high else 3, "tags": ["parachute"],
        "click": "https://weather.flybubble.com/",
    }, timeout=30).raise_for_status()


def main():
    today = date.today().isoformat()
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    prev_flyable = {k: {"status": "yes", **v}  # older state files have no status: they were all "yes"
                    for k, v in prev.get("flyable", {}).items() if k.split("|")[1] >= today}
    names = {slug: name for name, slug in SITES}

    flyable, errors, ok_slugs = {}, [], set()
    for name, slug in SITES:
        try:
            for row in fetch(slug):
                if row["status"] != "no" and row["date"] >= today:
                    flyable[key(slug, row)] = {"status": row["status"], "wind": row["wind"]}
            ok_slugs.add(slug)
        except Exception as e:
            print(f"{name}: {e}", file=sys.stderr)
            errors.append(name)
        time.sleep(2)

    # Keep previous entries for sites that failed, so they don't flap to NEW tomorrow
    for k, v in prev_flyable.items():
        if k.split("|")[0] not in ok_slugs:
            flyable.setdefault(k, v)

    # NEW = slot not listed last run, or its status changed (e.g. Marginal -> Flyable)
    new_keys = {k for k, v in flyable.items()
                if k not in prev_flyable or prev_flyable[k]["status"] != v["status"]}
    new_yes = {k for k in new_keys if flyable[k]["status"] == "yes"}
    dropped = {k for k in set(prev_flyable) - set(flyable) if k.split("|")[0] in ok_slugs}

    STATE_FILE.write_text(json.dumps({"updated": datetime.now().isoformat(timespec="seconds"),
                                      "flyable": flyable}, indent=1, sort_keys=True))

    if not (flyable or dropped or errors or ALWAYS_NOTIFY):
        print("Nothing flyable or marginal; no notification sent.")
        return
    ny = sum(v["status"] == "yes" for v in flyable.values())
    nm = len(flyable) - ny
    nn = len(new_keys)
    title = f"Flyable: {ny}, Marginal: {nm}" + (f" ({nn} new)" if nn else "")
    if not flyable:
        title = "Flyability update"
    notify(title, build_message(flyable, new_keys, dropped, errors, names), high=bool(new_yes))


if __name__ == "__main__":
    main()
