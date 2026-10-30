"""Shared client for NAV's job vacancy feed (pam-stilling-feed).

Docs: https://navikt.github.io/pam-stilling-feed/
Uses the rotating public token unless NAV_FEED_TOKEN is set (private consumer token).
"""
import json, os, re, time, urllib.request, urllib.error

BASE = "https://pam-stilling-feed.nav.no"
_token = os.environ.get("NAV_FEED_TOKEN")


def token(refresh=False):
    global _token
    if _token and not refresh:
        return _token
    txt = urllib.request.urlopen(BASE + "/api/publicToken", timeout=30).read().decode()
    _token = re.search(r"eyJ[\w.-]+", txt).group(0)
    return _token


def get(path, retries=8):
    for i in range(retries):
        req = urllib.request.Request(BASE + path, headers={
            "Authorization": "Bearer " + token(), "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                token(refresh=True)
            elif e.code == 404:
                return None
            time.sleep(min(2 ** i, 60))
        except Exception:
            time.sleep(min(2 ** i, 60))
    raise RuntimeError("failed: " + path)
