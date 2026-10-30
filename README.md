# NAV job ads 2019 to today

Every job ad in NAV's public job vacancy feed (arbeidsplassen.nav.no), crawled from the
[pam-stilling-feed API](https://navikt.github.io/pam-stilling-feed/). The feed covers ads from about
2019 onward. Ads that NAV gets from Finn.no are **not** included.

## What the feed gives you
* Every change to an ad is a feed item with: uuid, status, title, employer name, municipality, timestamp.
* Full content (description, orgnr, STYRK occupation, published/expiry date, location) is only
  available for ads that are **active** right now. NAV strips content from inactive ads.
* All ads from 2019 to June 2023 were bulk-loaded into the feed on 2023-06-14. Their real date
  comes from a per-ad lookup (`fetch_entries.py`), which returns the ad's original last-change time.
  That is roughly when the ad closed, typically a few weeks after publication.

## Pipeline
```
uv venv .venv && uv pip install --python .venv/bin/python pandas pyarrow
python scripts/crawl_feed.py 12                     # full feed, parallel time slices (~10 min)
python scripts/fetch_entries.py data/raw/bulk_uuids_shuffled.txt backfill_dates 24   # date 2019-23 ads (~3 h, resumable)
python scripts/fetch_entries.py data/raw/active_uuids.txt active_YYYYMMDD 8          # full content of active ads
.venv/bin/python scripts/build_dataset.py
.venv/bin/python scripts/match_portfolio.py portfolio.csv   # company,orgnr,aliases
```

## Outputs (`data/processed/`, git-ignored)
| file | content |
|---|---|
| `feed_snapshots.parquet` | every ad change in the feed |
| `ads.parquet` | one row per ad: title, employer, municipality, status, `date`, `date_basis` |
| `ads_active_full.parquet` | full ad text and metadata for ads active at crawl time |
| `portfolio_ads.parquet` | ads matched to portfolio companies |
| `portfolio_employer_matches.csv` | matched employer-name variants, to review for false positives |

`date_basis` is `first_seen_in_feed` (mid-2023 on, close to publication), `orig_last_change`
(2019 to mid-2023, close to ad closing) or `undated` (bulk-loaded ad not yet looked up).

Public token is fetched automatically and rotates. Set `NAV_FEED_TOKEN` to use a private consumer
token (email nav.team.arbeidsplassen@nav.no). Use is subject to NAV's
[API terms](https://arbeidsplassen.nav.no/vilkar-api).
