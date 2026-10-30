"""Crawl the full NAV job vacancy feed in parallel time slices.

The feed is a linked list of 1000-item pages. We jump into it at many points using the
If-Modified-Since header and follow next_url until the slice end, one thread per slice.
Every feed item is a snapshot of an ad at a change (uuid, status, title, employer,
municipality, timestamp). Overlaps between slices are removed in build_dataset.py.

Output: data/raw/feed/<slice_start>.jsonl.gz  (+ .done marker when the slice is complete)
"""
import gzip, json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
from navfeed import BASE, token, get

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "feed")
os.makedirs(OUT, exist_ok=True)
UTC = timezone.utc


def slices():
    # 2023-06-14 10:15-12:15 UTC: NAV bulk-loaded all historical ads (2019->) into the feed.
    t = datetime(2023, 6, 14, 10, 15, tzinfo=UTC)
    bf_end = datetime(2023, 6, 14, 12, 15, tzinfo=UTC)
    while t < bf_end:
        yield t, t + timedelta(minutes=5); t += timedelta(minutes=5)
    now = datetime.now(UTC)
    while t < now:
        yield t, min(t + timedelta(days=14), now + timedelta(days=1)); t += timedelta(days=14)


def first_page(start):
    hdr = {"Authorization": "Bearer " + token(), "Accept": "application/json",
           "If-Modified-Since": format_datetime(start, usegmt=True)}
    if start.year == 2023 and start.month == 6 and start.day == 14 and start.hour == 10 and start.minute == 15:
        return get("/api/v1/feed")  # very first page of the feed
    for i in range(6):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(BASE + "/api/v1/feed", headers=hdr), timeout=60).read())
        except Exception:
            token(refresh=True)
    raise RuntimeError(start)


def crawl(sl):
    start, end = sl
    name = start.strftime("%Y%m%dT%H%M")
    path = os.path.join(OUT, name + ".jsonl.gz")
    if os.path.exists(path + ".done"):
        return name, 0
    page, n = first_page(start), 0
    with gzip.open(path, "wt") as f:
        while page and page["items"]:
            stop = False
            for it in page["items"]:
                ts = datetime.fromisoformat(it["date_modified"])
                if ts >= end:
                    stop = True; break
                e = it["_feed_entry"]
                f.write(json.dumps({"uuid": e["uuid"], "status": e["status"], "title": e["title"],
                                    "businessName": e["businessName"], "municipal": e["municipal"],
                                    "ts": it["date_modified"]}, ensure_ascii=False) + "\n")
                n += 1
            if stop or not page.get("next_url"):
                break
            page = get(page["next_url"])
    open(path + ".done", "w").write(str(n))
    print(name, n, flush=True)
    return name, n


if __name__ == "__main__":
    sl = list(slices())
    print(len(sl), "slices", flush=True)
    with ThreadPoolExecutor(int(sys.argv[1]) if len(sys.argv) > 1 else 12) as ex:
        for _ in ex.map(crawl, sl):
            pass
    print("ALL DONE", flush=True)
