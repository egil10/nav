"""Build tidy datasets from the raw feed crawl.

Outputs (data/processed/):
  feed_snapshots.parquet  every distinct feed item (one row per ad change)
  ads.parquet             one row per ad (uuid) with best-known title/employer/municipality and a date
  ads_active_full.parquet full content for ads ACTIVE at crawl time (if fetched)

Dating:
  * Ads first seen after the 2023-06-14 bulk load: date = first time the ad appeared in the feed
    (~= publication).
  * Ads from the bulk load (2019 -> mid-2023): NAV re-stamped them all on 2023-06-14. Their original
    last-change time (~= when the ad closed) comes from /feedentry. We fetch it per ad
    (data/raw/entries/backfill_dates*), in random order so a partial run is a uniform random sample.
    Bulk-loaded ads not yet fetched are left undated (the bulk load is NOT in chronological order).
"""
import glob, gzip, json, os
import numpy as np, pandas as pd

ROOT = os.path.join(os.path.dirname(__file__), "..")
RAW, OUT = os.path.join(ROOT, "data", "raw"), os.path.join(ROOT, "data", "processed")
os.makedirs(OUT, exist_ok=True)
BACKFILL_END = pd.Timestamp("2023-06-14 12:15", tz="UTC")

# ---- snapshots -------------------------------------------------------------
frames = []
for f in sorted(glob.glob(os.path.join(RAW, "feed", "*.jsonl.gz"))):
    df = pd.read_json(f, lines=True, dtype=False, compression="gzip")
    if len(df):
        df["slice"] = os.path.basename(f)[:13]
        frames.append(df)
snap = pd.concat(frames, ignore_index=True)
snap["ts"] = pd.to_datetime(snap["ts"], utc=True, format="ISO8601")
snap = snap.drop_duplicates(["uuid", "ts", "status", "title", "businessName", "municipal"])
snap = snap.sort_values(["ts", "slice"], kind="stable").reset_index(drop=True)
snap["feed_pos"] = np.arange(len(snap))
snap["masked"] = snap["title"].eq("...") | snap["businessName"].fillna("").eq("")
snap.drop(columns=["slice"]).to_parquet(os.path.join(OUT, "feed_snapshots.parquet"), index=False)
print("snapshots", len(snap), "unique ads", snap.uuid.nunique())

# ---- one row per ad --------------------------------------------------------
g = snap.groupby("uuid", sort=False)
ads = pd.DataFrame({
    "first_feed_ts": g["ts"].min(), "last_feed_ts": g["ts"].max(),
    "first_feed_pos": g["feed_pos"].min(), "n_changes": g.size(),
    "status": g["status"].last(),
})
good = snap[~snap.masked].groupby("uuid").last()[["title", "businessName", "municipal"]]
ads = ads.join(good).rename(columns={"businessName": "employer", "municipal": "municipality"})
ads["from_bulk_load"] = ads["first_feed_ts"] < BACKFILL_END

# ---- dating ----------------------------------------------------------------
dates = []
for f in glob.glob(os.path.join(RAW, "entries", "backfill_dates*", "*.jsonl.gz")) + \
         glob.glob(os.path.join(RAW, "entries", "portfolio*", "*.jsonl.gz")):
    for l in gzip.open(f, "rt"):
        r = json.loads(l)
        if r.get("sistEndret"):
            dates.append((r["uuid"], r["sistEndret"]))
exact = pd.Series(dict(dates), dtype=object)
exact = pd.to_datetime(exact, utc=True, format="ISO8601")
ads["orig_last_change"] = exact.reindex(ads.index)

# bulk-loaded ads: exact original last-change time where fetched; otherwise undated
ads["date"] = ads["orig_last_change"].where(ads.from_bulk_load, ads["first_feed_ts"])
ads["date_basis"] = np.where(~ads.from_bulk_load, "first_seen_in_feed",
                     np.where(ads.orig_last_change.notna(), "orig_last_change", "undated"))
ads["year"] = ads["date"].dt.year
ads["month"] = ads["date"].dt.strftime("%Y-%m")
ads = ads.reset_index().rename(columns={"index": "uuid"})
ads.to_parquet(os.path.join(OUT, "ads.parquet"), index=False)
print(ads.groupby("year").size())

# ---- full content for currently active ads ---------------------------------
rows = []
for f in glob.glob(os.path.join(RAW, "entries", "active*", "*.jsonl.gz")):
    for l in gzip.open(f, "rt"):
        r = json.loads(l); c = r.get("ad_content")
        if not c:
            continue
        emp = c.get("employer") or {}
        loc = (c.get("workLocations") or [{}])[0]
        occ = (c.get("occupationCategories") or [{}])[0]
        styrk = next((x for x in c.get("categoryList") or [] if x.get("categoryType") == "STYRK08"), {})
        rows.append({"uuid": r["uuid"], "status": r.get("status"), "title": c.get("title"),
                     "jobtitle": c.get("jobtitle"), "employer": emp.get("name"), "orgnr": emp.get("orgnr"),
                     "published": c.get("published"), "expires": c.get("expires"), "updated": c.get("updated"),
                     "municipality": loc.get("municipal"), "county": loc.get("county"), "city": loc.get("city"),
                     "postal_code": loc.get("postalCode"), "occupation_l1": occ.get("level1"),
                     "occupation_l2": occ.get("level2"), "styrk08": styrk.get("code"), "styrk08_name": styrk.get("name"),
                     "engagementtype": c.get("engagementtype"), "extent": c.get("extent"),
                     "positioncount": c.get("positioncount"), "sector": c.get("sector"), "source": c.get("source"),
                     "link": c.get("link"), "description_html": c.get("description")})
if rows:
    full = pd.DataFrame(rows).drop_duplicates("uuid")
    full.to_parquet(os.path.join(OUT, "ads_active_full.parquet"), index=False)
    print("active full", len(full))
