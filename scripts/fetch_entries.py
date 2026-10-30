"""Fetch /feedentry details for a list of ad UUIDs (parallel, resumable).

For ACTIVE ads this returns the full ad (description, employer orgnr, occupation codes,
published/expires, location...). For INACTIVE ads NAV strips the content, but the
returned sistEndret is the ad's original last-change time, which dates ads that were
bulk-loaded into the feed on 2023-06-14.

usage: python scripts/fetch_entries.py <uuid_list.txt> <out_name> [threads]
Output: data/raw/entries/<out_name>/part_XXXX.jsonl.gz
"""
import gzip, json, os, sys, glob
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
from navfeed import get

src, name = sys.argv[1], sys.argv[2]
threads = int(sys.argv[3]) if len(sys.argv) > 3 else 16
out = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "entries", name)
os.makedirs(out, exist_ok=True)

done = set()
for f in glob.glob(os.path.join(out, "part_*.jsonl.gz")):
    try:
        for l in gzip.open(f, "rt"):
            done.add(json.loads(l)["uuid"])
    except Exception:
        os.remove(f)  # partial/corrupt chunk, redo
todo = [u.strip() for u in open(src) if u.strip() and u.strip() not in done]
print(f"{len(done)} done, {len(todo)} to fetch", flush=True)

def one(u):
    d = get("/api/v1/feedentry/" + u)
    return d if d else {"uuid": u, "missing": True}

CH = 5000
n0 = len(glob.glob(os.path.join(out, "part_*.jsonl.gz")))
with ThreadPoolExecutor(threads) as ex:
    for k in range(0, len(todo), CH):
        res = list(ex.map(one, todo[k:k + CH]))
        with gzip.open(os.path.join(out, f"part_{n0 + k // CH:05d}.jsonl.gz"), "wt") as f:
            for r in res:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{k + len(res)}/{len(todo)}", flush=True)
print("done")
