"""Find job ads for portfolio companies.

usage: python scripts/match_portfolio.py portfolio.csv
portfolio.csv columns: company[,orgnr][,aliases]   (aliases separated by ';')

Steps
  1. Build name patterns per company: company name + aliases, plus names of its Brønnøysund
     sub-units (underenheter) when an orgnr is given.
  2. Match against employer names in data/processed/ads.parquet (normalised, word-boundary).
  3. Write data/processed/portfolio_ads.parquet and a review file of matched employer names
     (data/processed/portfolio_employer_matches.csv) so false positives can be pruned.
  4. Write data/raw/portfolio_uuids.txt; run fetch_entries.py on it (out name 'portfolio') to
     give every undated 2019-2023 portfolio ad an exact date, then rebuild.
"""
import json, re, sys, os, unicodedata, urllib.request
import pandas as pd

ROOT = os.path.join(os.path.dirname(__file__), "..")
P = os.path.join(ROOT, "data", "processed")
SUFFIX = r"\b(asa|as|sa|ab|ans|da|ltd|gmbh|norge|norway|avd|avdeling|holding|group|gruppen)\b"


def norm(s):
    s = unicodedata.normalize("NFKD", str(s).lower()).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def core(s):
    return re.sub(r"\s+", " ", re.sub(SUFFIX, " ", norm(s))).strip()


def brreg_subunits(orgnr):
    names, page = [], 0
    while True:
        url = f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={orgnr}&size=200&page={page}"
        d = json.loads(urllib.request.urlopen(url, timeout=30).read())
        names += [u["navn"] for u in d.get("_embedded", {}).get("underenheter", [])]
        if page + 1 >= d["page"]["totalPages"]:
            return names
        page += 1


pf = pd.read_csv(sys.argv[1], dtype=str).fillna("")
ads = pd.read_parquet(os.path.join(P, "ads.parquet"))
emp = pd.Series(ads["employer"].dropna().unique())
emp_n = emp.map(norm)

hits = []
for _, r in pf.iterrows():
    names = [r["company"]] + [a for a in r.get("aliases", "").split(";") if a.strip()]
    if r.get("orgnr"):
        try:
            names += brreg_subunits(re.sub(r"\D", "", r["orgnr"]))
        except Exception as e:
            print("brreg failed", r["orgnr"], e)
    pats = sorted({core(n) for n in names if len(core(n)) >= 3})
    rx = re.compile(r"\b(?:" + "|".join(map(re.escape, pats)) + r")\b")
    m = emp[emp_n.str.contains(rx)]
    hits += [{"company": r["company"], "employer": e} for e in m]

hm = pd.DataFrame(hits, columns=["company", "employer"])
cnt = ads.groupby("employer").size().rename("n_ads")
hm.join(cnt, on="employer").sort_values(["company", "n_ads"], ascending=[True, False]) \
  .to_csv(os.path.join(P, "portfolio_employer_matches.csv"), index=False)
out = ads.merge(hm, on="employer")
out.to_parquet(os.path.join(P, "portfolio_ads.parquet"), index=False)
out.loc[out.date_basis.eq("undated"), "uuid"].drop_duplicates() \
   .to_csv(os.path.join(ROOT, "data", "raw", "portfolio_uuids.txt"), index=False, header=False)
print(out.groupby(["company", "year"], dropna=False).size().unstack(fill_value=0))
