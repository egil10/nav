"""Job-ad signal vs stock returns for the equity universe in investments-equities.xlsx.

Inputs
  data/processed/ads.parquet               (build_dataset.py)
  mapping/group_*.json                     company -> Norwegian employer names + Yahoo ticker
Outputs
  data/processed/universe_monthly.parquet  company x month: ads, signal, returns
  data/processed/dashboard_data.json       everything the dashboard needs

Method
  * Ad date: first appearance in the feed (mid-2023 on). Ads bulk-loaded for 2019-mid 2023 carry
    their closing date, so we shift them back by the median ad lifetime (25 days) to approximate
    publication. Only part of the bulk ads are dated yet; they were dated in random order, so
    monthly counts are scaled up by 1 / dated share (same factor for every company and month).
  * Signal: ad momentum = log((ads last 3m + 1) / (ads same 3m a year earlier + 1)) minus the same
    quantity for the whole Norwegian ad market (removes seasonality and the macro cycle).
  * Returns: monthly total-return proxy from Yahoo adjusted close, local currency, in excess of
    the equal-weighted universe average.
  * Tests: monthly cross-sectional Spearman rank IC vs forward 1/3/6m excess returns,
    tercile long-short portfolio, pooled scatter, lead/lag profile.
"""
import glob, json, os, re, warnings
import numpy as np, pandas as pd
from scipy import stats
warnings.filterwarnings("ignore")

ROOT = os.path.join(os.path.dirname(__file__), "..")
P = os.path.join(ROOT, "data", "processed")
SHIFT = pd.Timedelta(days=25)
MIN_ADS_12M = 8            # company needs this many ads in trailing 12m to enter a cross-section
START = pd.Timestamp("2019-01-01")

# ---------------------------------------------------------------- mapping
uni = pd.read_excel(os.path.join(ROOT, "investments-equities.xlsx"))
maps = []
for f in sorted(glob.glob(os.path.join(ROOT, "mapping", "group_*.json"))):
    maps += json.load(open(f))
mp = pd.DataFrame(maps).drop_duplicates("name").set_index("name")
uni = uni.join(mp[["ticker", "norway_presence", "employers", "regex", "notes"]], on="name")

# ---------------------------------------------------------------- ads
ads = pd.read_parquet(os.path.join(P, "ads.parquet"))
ads = ads[ads.employer.notna()].copy()
ads["d"] = ads["date"].dt.tz_localize(None)
ads.loc[ads.from_bulk_load, "d"] -= SHIFT
ads["m"] = ads["d"].dt.to_period("M")
emp_low = ads["employer"].str.lower().str.strip()

rows = []
for _, r in uni.iterrows():
    exact = set(e.lower().strip() for e in (r["employers"] if isinstance(r["employers"], list) else []))
    mask = emp_low.isin(exact)
    for rx in (r["regex"] if isinstance(r["regex"], list) else []) or []:
        try:
            mask |= emp_low.str.contains(rx, flags=re.I, regex=True)
        except re.error:
            print("bad regex", r["name"], rx)
    hit = ads[mask]
    if len(hit):
        rows.append(hit.assign(company=r["name"]))
m_ads = pd.concat(rows) if rows else pd.DataFrame(columns=list(ads.columns) + ["company"])
# weights: universe ads are all dated individually (w=1). Other bulk ads were dated in random
# order, so market totals scale the dated ones up by 1 / dated share.
in_uni = ads.uuid.isin(m_ads.uuid)
bulk_other = ads.from_bulk_load & ~in_uni
dated_share = ads.loc[bulk_other, "d"].notna().mean()
ads["w"] = np.where(bulk_other, 1 / dated_share, 1.0)
m_ads["w"] = 1.0
print("undated universe ads:", int(m_ads.d.isna().sum()), "of", len(m_ads), "| bulk dated share", round(dated_share, 3))
m_ads[["uuid", "company", "employer", "title", "municipality", "status", "date", "date_basis", "from_bulk_load"]] \
    .to_parquet(os.path.join(P, "universe_ads.parquet"), index=False)
# undated portfolio ads -> list so they can be dated first
m_ads.loc[m_ads.date.isna(), "uuid"].drop_duplicates().to_csv(
    os.path.join(ROOT, "data", "raw", "portfolio_uuids.txt"), index=False, header=False)

months = pd.period_range("2019-01", pd.Timestamp.today().to_period("M"), freq="M")
cnt = m_ads.dropna(subset=["m"]).groupby(["company", "m"])["w"].sum().unstack(fill_value=0)
cnt = cnt.reindex(columns=months, fill_value=0)
mkt = ads.dropna(subset=["m"]).groupby("m")["w"].sum().reindex(months, fill_value=0)

# ---------------------------------------------------------------- prices
import yfinance as yf
tick = uni.dropna(subset=["ticker"]).set_index("name")["ticker"]
tick = tick[tick.astype(str).str.len() > 0]
px = yf.download(sorted(set(tick)), start="2018-01-01", interval="1mo", auto_adjust=True,
                 progress=False, threads=True)["Close"]
px.index = pd.to_datetime(px.index).to_period("M")
px = px.groupby(level=0).last()
px.to_parquet(os.path.join(P, "prices_monthly.parquet"))
ret = np.log(px).diff()
ret = ret.where(ret.abs() < 1.5)  # drop broken prints

# ---------------------------------------------------------------- panel
def rolling3(s): return s.T.rolling(3).sum().T
c3, m3 = rolling3(cnt), mkt.rolling(3).sum()
mom = np.log((c3 + 1) / (c3.shift(12, axis=1) + 1)) - np.log((m3 + 1) / (m3.shift(12) + 1))
ads12 = cnt.T.rolling(12).sum().T
panel = []
for comp in cnt.index:
    t = tick.get(comp)
    if t is None or t not in ret.columns:
        continue
    r = ret[t].reindex(months)
    df = pd.DataFrame({"company": comp, "ticker": t, "m": months, "ads": cnt.loc[comp].values,
                       "ads12": ads12.loc[comp].values, "signal": mom.loc[comp].values, "ret": r.values})
    panel.append(df)
panel = pd.concat(panel, ignore_index=True)
ew = panel.groupby("m")["ret"].mean()
panel["xret"] = panel["ret"] - panel["m"].map(ew)
g = panel.groupby("company")["xret"]
for h in (1, 3, 6, 12):
    panel[f"fwd{h}"] = g.transform(lambda s: s[::-1].rolling(h).sum()[::-1].shift(-1))
panel["past3"] = g.transform(lambda s: s.rolling(3).sum())
panel["past12"] = g.transform(lambda s: s.rolling(12).sum())
panel["elig"] = (panel.ads12 >= MIN_ADS_12M) & panel.signal.notna()
panel.to_parquet(os.path.join(P, "universe_monthly.parquet"), index=False)

# ---------------------------------------------------------------- tests
def ic_series(col):
    out = {}
    for m, d in panel[panel.elig].groupby("m"):
        d = d.dropna(subset=[col])
        if len(d) >= 8:
            out[m] = stats.spearmanr(d.signal, d[col]).correlation
    return pd.Series(out)

def tstat(s, h):  # Newey-West-ish: shrink by overlap
    s = s.dropna(); n = len(s)
    return float(s.mean() / (s.std() / np.sqrt(max(n / h, 1)))) if n > 2 else np.nan

ics = {h: ic_series(f"fwd{h}") for h in (1, 3, 6, 12)}
ic_tab = [{"horizon": h, "mean_ic": float(s.mean()), "t": tstat(s, h), "hit": float((s > 0).mean()), "n_months": int(s.notna().sum())}
          for h, s in ics.items()]
ic_contemp = ic_series("past3")

# lead/lag: IC of signal vs 1m excess return k months ahead (negative = past)
leadlag = []
for k in range(-12, 13):
    panel["_r"] = panel.groupby("company")["xret"].shift(-k)
    s = ic_series("_r")
    leadlag.append({"k": k, "ic": float(s.mean()), "t": tstat(s, 1)})
panel.drop(columns="_r", inplace=True)

# tercile long-short on 1m forward
ls = []
for m, d in panel[panel.elig].dropna(subset=["fwd1"]).groupby("m"):
    if len(d) < 9: continue
    q = pd.qcut(d.signal.rank(method="first"), 3, labels=False)
    ls.append({"m": str(m), "long": d.fwd1[q == 2].mean(), "short": d.fwd1[q == 0].mean(), "n": len(d)})
ls = pd.DataFrame(ls)
ls["ls"] = ls["long"] - ls["short"]
ls["cum"] = ls["ls"].cumsum()
ls_stats = {"ann_ret": float(ls.ls.mean() * 12), "ann_vol": float(ls.ls.std() * np.sqrt(12)),
            "sharpe": float(ls.ls.mean() / ls.ls.std() * np.sqrt(12)), "t": float(ls.ls.mean() / ls.ls.std() * np.sqrt(len(ls))),
            "hit": float((ls.ls > 0).mean()), "months": int(len(ls)), "avg_names": float(ls.n.mean())}

# pooled scatter: non-overlapping 6m windows (Jan/Jul) of signal vs forward 6m excess
sc = panel[panel.elig & panel.fwd6.notna() & panel.m.dt.month.isin([1, 7])]
pooled = stats.spearmanr(sc.signal, sc.fwd6)
# annual scatter: yoy ad growth vs same-year return (contemporaneous)
yr = panel.assign(y=panel.m.dt.year).groupby(["company", "y"]).agg(ads=("ads", "sum"), xret=("xret", "sum"), n=("xret", "count")).reset_index()
yr["mkt"] = yr.y.map(mkt.groupby(mkt.index.year).sum())
yr["share"] = yr.ads / yr.mkt
yr["g"] = yr.groupby("company")["share"].transform(lambda s: np.log((s + 1e-7) / (s.shift(1) + 1e-7)))
ya = yr[(yr.ads >= 10) & yr.g.notna() & (yr.n >= 10) & yr.groupby("company").ads.shift(1).ge(10)]
annual = stats.spearmanr(ya.g, ya.xret) if len(ya) > 5 else None

# robustness: subperiods and eligibility thresholds (3m horizon IC, 1m L/S)
def run(sub, h=3):
    out = []
    for m, d in sub.groupby("m"):
        d = d.dropna(subset=[f"fwd{h}"])
        if len(d) >= 8:
            out.append(stats.spearmanr(d.signal, d[f"fwd{h}"]).correlation)
    s_ = pd.Series(out)
    return {"mean_ic": float(s_.mean()), "t": tstat(s_, h), "n_months": len(s_)}
robust = []
base = panel[panel.signal.notna()]
for lab, cond in [("All months", base.m >= pd.Period("2019-01")),
                  ("2020 to mid-2023 (bulk-loaded ads)", base.m < pd.Period("2023-07")),
                  ("Mid-2023 to today (live feed)", base.m >= pd.Period("2023-07"))]:
    for k in (5, 8, 15):
        r = run(base[cond & (base.ads12 >= k)]); r.update({"sample": lab, "min_ads": k}); robust.append(r)

# ---------------------------------------------------------------- export
def per(m): return str(m)
comp_tab = []
for _, r in uni.iterrows():
    n = int(round(cnt.loc[r["name"]].sum())) if r["name"] in cnt.index else 0
    last12 = float(cnt.loc[r["name"]].iloc[-12:].sum()) if r["name"] in cnt.index else 0
    prev12 = float(cnt.loc[r["name"]].iloc[-24:-12].sum()) if r["name"] in cnt.index else 0
    sig = panel[(panel.company == r["name"])].dropna(subset=["signal"]).tail(1)
    comp_tab.append({"name": r["name"], "ticker": r.get("ticker"), "sector": r["sector"], "country": r["country"],
                     "mv": float(r["marketValue"]), "presence": r.get("norway_presence"), "ads_total": n,
                     "ads_12m": round(last12), "ads_prev12m": round(prev12),
                     "signal": None if sig.empty or last12 < MIN_ADS_12M else round(float(sig.signal.iloc[0]), 3),
                     "employers": (r["employers"] if isinstance(r["employers"], list) else [])[:8],
                     "notes": r.get("notes")})
series = {}
for comp in cnt.index:
    if cnt.loc[comp].sum() < 30: continue
    t = tick.get(comp)
    p = px[t].reindex(months) if t in px.columns else pd.Series(index=months, dtype=float)
    series[comp] = {"ticker": t, "ads": [round(float(x), 1) for x in cnt.loc[comp].values],
                    "price": [None if pd.isna(x) else round(float(x), 3) for x in p.values]}
scatter = [{"c": r.company, "m": per(r.m), "x": round(float(r.signal), 3), "y": round(float(r.fwd6), 4)} for r in sc.itertuples()]
annual_pts = [{"c": r.company, "y": int(r.y), "x": round(float(r.g), 3), "r": round(float(r.xret), 4), "ads": int(r.ads)} for r in ya.itertuples()]
out = {
    "generated": pd.Timestamp.now().isoformat(timespec="minutes"),
    "months": [per(m) for m in months],
    "market_ads": [round(float(x)) for x in mkt.values],
    "dated_share_bulk": round(float(dated_share), 4),
    "companies": comp_tab, "series": series,
    "ic": ic_tab, "ic_monthly_3m": [{"m": per(k), "ic": round(float(v), 3)} for k, v in ics[3].items()],
    "ic_contemp_mean": float(ic_contemp.mean()),
    "leadlag": leadlag, "ls": ls.round(5).to_dict("records"), "ls_stats": ls_stats,
    "scatter": scatter, "scatter_rho": float(pooled.correlation), "scatter_p": float(pooled.pvalue), "scatter_n": len(sc),
    "annual": annual_pts, "annual_rho": None if annual is None else float(annual.correlation),
    "annual_p": None if annual is None else float(annual.pvalue),
    "robust": robust,
    "params": {"min_ads_12m": MIN_ADS_12M, "shift_days": 25},
}
json.dump(out, open(os.path.join(P, "dashboard_data.json"), "w"), default=lambda o: None if o is None or (isinstance(o, float) and np.isnan(o)) else str(o))
print(pd.DataFrame(ic_tab)); print(ls_stats); print("scatter", pooled, len(sc)); print("annual", annual, len(ya))
print(pd.DataFrame(robust)); print("companies with ads:", (cnt.sum(axis=1) > 0).sum(), " in panel:", panel.company.nunique())

# ---------------------------------------------------------------- residual vs price momentum
res = []
for m, d in panel[panel.elig].dropna(subset=["fwd3", "past3"]).groupby("m"):
    if len(d) < 8: continue
    b = np.polyfit(d.past3, d.signal, 1)
    resid = d.signal - np.polyval(b, d.past3)
    res.append(stats.spearmanr(resid, d.fwd3).correlation)
res = pd.Series(res)
robust.append({"sample": "All months, net of 3m price momentum", "min_ads": MIN_ADS_12M,
               "mean_ic": float(res.mean()), "t": tstat(res, 3), "n_months": len(res)})

# ---------------------------------------------------------------- dashboard dataset files
D = os.path.join(P, "dashboard"); os.makedirs(D, exist_ok=True)
ic1, ic3 = ic_tab[0], ic_tab[1]
pd.DataFrame([{
    "ic_1m": ic1["mean_ic"], "t_1m": ic1["t"], "hit_1m": ic1["hit"], "ic_3m": ic3["mean_ic"], "t_3m": ic3["t"],
    "ls_ann_ret": ls_stats["ann_ret"], "ls_sharpe": ls_stats["sharpe"], "ls_t": ls_stats["t"], "ls_hit": ls_stats["hit"],
    "ls_months": ls_stats["months"], "avg_names": ls_stats["avg_names"],
    "companies_total": len(uni), "companies_with_ads": int((cnt.sum(axis=1) > 0).sum()),
    "companies_in_test": int(panel[panel.elig].company.nunique()), "ads_matched": int(len(m_ads)),
    "scatter_rho": float(pooled.correlation), "scatter_p": float(pooled.pvalue), "scatter_n": len(sc),
    "annual_rho": float(annual.correlation), "annual_p": float(annual.pvalue), "annual_n": len(ya),
    "first_month": str(ls.m.iloc[0]), "last_month": str(ls.m.iloc[-1]),
}]).round(4).to_csv(os.path.join(D, "headline.csv"), index=False)
pd.DataFrame(ic_tab).round(4).assign(label=lambda d: d.horizon.astype(str) + "m") \
    .to_csv(os.path.join(D, "ic_by_horizon.csv"), index=False)
pd.DataFrame(leadlag).round(4).to_csv(os.path.join(D, "lead_lag.csv"), index=False)
ls.round(5).to_csv(os.path.join(D, "long_short.csv"), index=False)
sc.assign(month=sc.m.astype(str))[["company", "ticker", "month", "signal", "fwd6"]].round(4) \
    .to_csv(os.path.join(D, "scatter.csv"), index=False)
ya.rename(columns={"y": "year", "g": "ad_share_growth"})[["company", "year", "ads", "ad_share_growth", "xret"]].round(4) \
    .to_csv(os.path.join(D, "annual.csv"), index=False)
pd.DataFrame(comp_tab).drop(columns=["employers", "notes"]).assign(
    employers=[", ".join(c["employers"]) for c in comp_tab]).to_csv(os.path.join(D, "companies.csv"), index=False)
mon = []
for comp, v in series.items():
    for mm, a, p in zip(out["months"], v["ads"], v["price"]):
        mon.append({"company": comp, "month": mm, "ads": a, "price": p})
pd.DataFrame(mon).to_csv(os.path.join(D, "company_monthly.csv"), index=False)
pd.DataFrame({"month": out["months"], "ads": out["market_ads"]}).to_csv(os.path.join(D, "market.csv"), index=False)
pd.DataFrame(robust).round(4).to_csv(os.path.join(D, "robustness.csv"), index=False)
print(pd.DataFrame(robust).tail(1)); print(os.listdir(D))
