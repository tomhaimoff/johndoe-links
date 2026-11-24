#!/usr/bin/env python3
"""
Pulls John Doe's upcoming events (and their posters) from go-out and writes them into the page.

Runs every hour on GitHub (see .github/workflows/deploy.yml), and can be run by hand:
    python3 scripts/update_events.py          # update site/index.html, site/events.json, poster thumbnails
    python3 scripts/update_events.py --check  # only print what it found, change nothing

Events that go-out doesn't show on the venue page can be added by link in scripts/extra_events.txt.

If go-out can't be read, the script exits with an error and leaves the page as it was,
so the last working ticket buttons stay up.
"""
import html
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

VENUE_URL = "https://go-out.co/venue/john-doe"
EVENT_URL = "https://www.go-out.co/event/{id}?ref=a39f22233"  # ref = go-out referral code from the old Linktree
POSTER_URL = "https://images.go-out.co/events/{oid}{ts}_coverImage.jpg"
EVENT_PAGE = "https://go-out.co/event/{id}"
TZ = ZoneInfo("Asia/Jerusalem")
THUMB_PX = 144  # shown at 48px, sharp on 3x phone screens

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
INDEX = SITE / "index.html"
EVENTS_JSON = SITE / "events.json"
EXTRA = ROOT / "scripts" / "extra_events.txt"
THUMBS = SITE / "assets" / "events"
FALLBACK_THUMB = "assets/logo.jpg"
START_MARK, END_MARK = "<!-- EVENTS:START -->", "<!-- EVENTS:END -->"
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,he;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_local(value: str) -> datetime:
    """go-out dates look like '2026-11-24T22:30:00.000' in Israel time."""
    value = str(value).strip().rstrip("Z").split(".")[0]
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=TZ)


def page_props(page: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        raise RuntimeError("go-out page changed: no __NEXT_DATA__ block")
    return json.loads(m.group(1)).get("props", {}).get("pageProps", {})


def venue_events(page: str) -> list:
    raw = page_props(page).get("events")
    if not isinstance(raw, list):
        raise RuntimeError("go-out page changed: no events list")
    return raw


def extra_events() -> list:
    """Events listed by hand in scripts/extra_events.txt (ones go-out forgot to show on the venue page)."""
    if not EXTRA.exists():
        return []
    raw = []
    for line in EXTRA.read_text(encoding="utf-8").splitlines():
        m = re.search(r"/event/(\d{6,20})", line.split("#", 1)[0])
        if not m:
            continue
        try:  # a deleted or broken event shouldn't break the page
            ev = page_props(fetch(EVENT_PAGE.format(id=m.group(1))).decode("utf-8")).get("event")
            if isinstance(ev, dict) and ev.get("Active", True):
                raw.append(ev)
        except Exception as exc:
            print(f"WARNING: could not read extra event {m.group(1)}: {exc}", file=sys.stderr)
    return raw


def read_events(raw: list) -> list:
    now = datetime.now(TZ)
    events, seen = [], set()
    for ev in raw:
        event_id = str(ev.get("Url", "")).strip()
        if not re.fullmatch(r"\d{6,20}", event_id) or event_id in seen:
            continue
        start = parse_local(ev["StartingDate"])
        end = parse_local(ev["EndingDate"]) if ev.get("EndingDate") else start + timedelta(hours=8)
        if end <= now:
            continue
        seen.add(event_id)
        night = (start - timedelta(hours=6)).date()  # a 00:30 start still belongs to the night before
        oid, ts = str(ev.get("_id", "")), str(ev.get("CoverImageTimestamp", ""))
        poster = POSTER_URL.format(oid=oid, ts=ts) if re.fullmatch(r"[0-9a-f]{24}", oid) and ts.isdigit() else None
        events.append(
            {
                "id": event_id,
                # " ".join tidies spaces; the sub turns a typed "Friday I John Doe" into "Friday | John Doe"
                "title": re.sub(r"^(\w+day) I ", r"\1 | ", " ".join(str(ev.get("Title", "")).split())),
                "night": night.isoformat(),
                "date": f"{night.day}.{night.month}",
                "time": start.strftime("%H:%M"),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "url": EVENT_URL.format(id=event_id),
                "poster": poster,
                "thumb": f"assets/events/{event_id}-{ts}.jpg" if poster else FALLBACK_THUMB,
            }
        )
    events.sort(key=lambda e: e["start"])
    return events


def make_thumb(data: bytes, dest: Path) -> None:
    """Shrink a poster to a small square JPEG (Pillow on GitHub, sips on a Mac)."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((THUMB_PX, THUMB_PX))
        img.save(dest, "JPEG", quality=82, optimize=True, progressive=True)
        return
    except ImportError:
        pass
    if shutil.which("sips"):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            tmp.write(data)
            tmp.flush()
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82", "-Z", str(THUMB_PX), tmp.name, "--out", str(dest)],
                check=True,
                capture_output=True,
            )
        return
    raise RuntimeError("no image tool available (install Pillow)")


def sync_thumbs(events: list) -> bool:
    """Download posters for new events, delete posters of past events. Returns True if anything changed."""
    THUMBS.mkdir(parents=True, exist_ok=True)
    changed = False
    wanted = set()
    for e in events:
        if not e["poster"]:
            continue
        dest = SITE / e["thumb"]
        wanted.add(dest.name)
        if dest.exists():
            continue
        try:
            make_thumb(fetch(e["poster"]), dest)
            changed = True
        except Exception as exc:  # a missing poster shouldn't break the page
            print(f"WARNING: no poster for {e['title']}: {exc}", file=sys.stderr)
            e["thumb"] = FALLBACK_THUMB
    for old in THUMBS.glob("*.jpg"):
        if old.name not in wanted:
            old.unlink()
            changed = True
    return changed


def render(events: list) -> str:
    if not events:
        return "\n"  # the page shows its own "nothing published yet" note
    rows = []
    for e in events:
        rows.append(
            f'<a class="card" href="{html.escape(e["url"])}" target="_blank" rel="noopener" '
            f'data-night="{e["night"]}" data-end="{e["end"]}">'
            f'<img class="thumb" src="{e["thumb"]}" alt="" width="48" height="48" loading="lazy">'
            f'<span class="card-text"><span class="card-title">{html.escape(e["title"])}</span>'
            f'<span class="card-sub">{e["date"]} · Doors {e["time"]}</span></span></a>'
        )
    return "\n" + "\n".join(rows) + "\n"


def main() -> int:
    check_only = "--check" in sys.argv
    try:
        events = read_events(venue_events(fetch(VENUE_URL).decode("utf-8")) + extra_events())
    except Exception as exc:  # leave the page untouched
        print(f"ERROR: could not read events from go-out: {exc}", file=sys.stderr)
        return 1

    for e in events:
        print(f'{e["date"]} {e["time"]}  {e["title"]}  ->  {e["url"]}')
    if not events:
        print("No upcoming events on go-out right now.")
    if check_only:
        return 0

    changed = sync_thumbs(events)

    page = INDEX.read_text(encoding="utf-8")
    a, b = page.find(START_MARK), page.find(END_MARK)
    if a == -1 or b == -1 or b < a:
        print("ERROR: EVENTS markers missing from site/index.html", file=sys.stderr)
        return 1
    new_page = page[: a + len(START_MARK)] + render(events) + page[b:]
    new_json = json.dumps(events, ensure_ascii=False, indent=2) + "\n"

    if new_page != page:
        INDEX.write_text(new_page, encoding="utf-8")
        changed = True
    if not EVENTS_JSON.exists() or EVENTS_JSON.read_text(encoding="utf-8") != new_json:
        EVENTS_JSON.write_text(new_json, encoding="utf-8")
        changed = True
    print("Page updated." if changed else "No changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
