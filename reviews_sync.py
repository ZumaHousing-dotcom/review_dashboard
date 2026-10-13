#!/usr/bin/env python3
"""Fetch Zuma Housing reviews (Google, Yelp, AppFolio) and load them into the
Review Insights dashboard.

Usage:
    python reviews_sync.py                       # fetch all, write reviews.json
    python reviews_sync.py --html "Review Insights v2.dc.html"   # also inject into the design
    python reviews_sync.py --sources google,yelp  # subset
    python reviews_sync.py --appfolio-csv survey.csv

Configuration comes from environment variables / a .env file (see .env.example).
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

START, END = "/*REVIEWS_DATA_START*/", "/*REVIEWS_DATA_END*/"
TIMEOUT = 30


def load_env(path=".env"):
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def need(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env var {name}")
    return v


def review(source, rid, author, rating, text, date, url=None, reply=None, category=None):
    return {"category": category, "id": f"{source}:{rid}", "source": source, "author": author or "Anonymous",
            "rating": float(rating) if rating is not None else None,
            "text": (text or "").strip(), "date": date, "url": url, "reply": reply}


# ---------------------------------------------------------------- Google
STAR = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def fetch_google():
    """Google Business Profile API (OAuth refresh-token flow). Requires API access
    approval for your GBP account. GOOGLE_LOCATION is 'accounts/123/locations/456'."""
    tok = requests.post("https://oauth2.googleapis.com/token", timeout=TIMEOUT, data={
        "client_id": need("GOOGLE_CLIENT_ID"), "client_secret": need("GOOGLE_CLIENT_SECRET"),
        "refresh_token": need("GOOGLE_REFRESH_TOKEN"), "grant_type": "refresh_token"})
    tok.raise_for_status()
    headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}
    url = f"https://mybusiness.googleapis.com/v4/{need('GOOGLE_LOCATION')}/reviews"
    out, params = [], {"pageSize": 50}
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        for x in d.get("reviews", []):
            out.append(review("google", x["reviewId"], x.get("reviewer", {}).get("displayName"),
                              STAR.get(x.get("starRating")), x.get("comment"), x.get("createTime"),
                              reply=x.get("reviewReply", {}).get("comment")))
        if not d.get("nextPageToken"):
            return out
        params["pageToken"] = d["nextPageToken"]


def fetch_google_serpapi():
    """Google reviews via SerpApi (no Google approval needed). Needs SERPAPI_KEY and
    either SERPAPI_PLACE_ID (starts with ChIJ) or SERPAPI_DATA_ID."""
    params = {"engine": "google_maps_reviews", "api_key": need("SERPAPI_KEY"),
              "sort_by": "newestFirst", "hl": "en"}
    if os.environ.get("SERPAPI_PLACE_ID"):
        params["place_id"] = os.environ["SERPAPI_PLACE_ID"]
    else:
        params["data_id"] = need("SERPAPI_DATA_ID")
    out = []
    while True:
        r = requests.get("https://serpapi.com/search.json", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise RuntimeError(d["error"])
        for x in d.get("reviews", []):
            out.append(review("google", x.get("review_id") or x.get("link"),
                              x.get("user", {}).get("name"), x.get("rating"),
                              x.get("extracted_snippet", {}).get("original") or x.get("snippet"),
                              x.get("iso_date") or x.get("date"), x.get("link"),
                              (x.get("response") or {}).get("extracted_snippet", {}).get("original")
                              or (x.get("response") or {}).get("snippet")))
        token = d.get("serpapi_pagination", {}).get("next_page_token")
        if not token:
            return out
        params["next_page_token"] = token
        params["num"] = 20


def find_place(name):
    """Print the IDs for a business so you can paste them into .env."""
    r = requests.get("https://serpapi.com/search.json", timeout=TIMEOUT, params={
        "engine": "google_maps", "q": name, "type": "search", "api_key": need("SERPAPI_KEY")})
    r.raise_for_status()
    d = r.json()
    hits = d.get("local_results") or ([d["place_results"]] if d.get("place_results") else [])
    for h in hits[:5]:
        print(f"{h.get('title')} | {h.get('address')}\n  SERPAPI_PLACE_ID={h.get('place_id')}\n  SERPAPI_DATA_ID={h.get('data_id')}")
    if not hits:
        print("No matches found. Try adding the city, e.g. 'Zuma Housing Austin TX'.")


# ------------------------------------------------------------------ Yelp
def fetch_yelp():
    """Yelp Fusion API. NOTE: Yelp only returns up to 3 review excerpts per business."""
    r = requests.get(f"https://api.yelp.com/v3/businesses/{need('YELP_BUSINESS_ID')}/reviews",
                     headers={"Authorization": f"Bearer {need('YELP_API_KEY')}"},
                     params={"limit": 50, "sort_by": "newest"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [review("yelp", x["id"], x.get("user", {}).get("name"), x.get("rating"),
                   x.get("text"), x.get("time_created"), x.get("url"))
            for x in r.json().get("reviews", [])]


# -------------------------------------------------------------- AppFolio
def _pick(row, *names):
    low = {k.strip().lower(): v for k, v in row.items() if k}
    for n in names:
        if low.get(n):
            return low[n]
    return None


def fetch_appfolio(csv_path=None):
    """AppFolio has no public survey-review API. Either export the survey results
    as CSV (--appfolio-csv / APPFOLIO_CSV), or set APPFOLIO_URL to an authenticated
    JSON/CSV report endpoint plus APPFOLIO_COOKIE or APPFOLIO_TOKEN."""
    csv_path = csv_path or os.environ.get("APPFOLIO_CSV")
    if csv_path:
        text = Path(csv_path).read_text(encoding="utf-8-sig")
    else:
        h = {}
        if os.environ.get("APPFOLIO_TOKEN"):
            h["Authorization"] = f"Bearer {os.environ['APPFOLIO_TOKEN']}"
        if os.environ.get("APPFOLIO_COOKIE"):
            h["Cookie"] = os.environ["APPFOLIO_COOKIE"]
        r = requests.get(need("APPFOLIO_URL"), headers=h, timeout=TIMEOUT)
        r.raise_for_status()
        if "json" in r.headers.get("content-type", ""):
            rows = r.json()
            rows = rows.get("results", rows) if isinstance(rows, dict) else rows
            return [_af_row(i, x) for i, x in enumerate(rows)]
        text = r.text
    return [_af_row(i, row) for i, row in enumerate(csv.DictReader(text.splitlines()))]


def _af_row(i, row):
    rating = _pick(row, "rating", "score", "overall rating", "overall", "stars", "overall_rating", "overall_score", "satisfaction", "nps")
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        rating = None
    return review("appfolio", _pick(row, "id", "survey id") or i,
                  _pick(row, "name", "resident", "tenant", "author"), rating,
                  _pick(row, "comment", "comments", "review", "feedback", "text", "additional_comments", "open_response"),
                  _pick(row, "date", "submitted", "submitted at", "completed", "created", "created_at", "submitted_at", "completed_at"))


def _is_test(row):
    """True for test entries: a truthy is_test/test column, or 'test' as a whole word in a
    name/email/comment-style field."""
    for k, v in row.items():
        k = (k or "").lower()
        if k in ("is_test", "test", "testing") and str(v).lower() in ("true", "1", "t", "yes"):
            return True
        if any(w in k for w in ("name", "email", "resident", "tenant", "author")) \
                and re.search(r"\btest(er|ing)?\b", str(v or ""), re.I):
            return True
    return False


def fetch_supabase():
    """AppFolio survey data stored in Supabase. Needs SUPABASE_URL, SUPABASE_KEY and
    SUPABASE_TABLE (one table, or several separated by commas). Test entries are skipped."""
    base = need("SUPABASE_URL").rstrip("/")
    key = need("SUPABASE_KEY")
    headers = {"apikey": key}
    if key.startswith("eyJ"):  # old-style JWT keys also go in Authorization; new sb_* keys must not
        headers["Authorization"] = f"Bearer {key}"
    out = []
    for table in [t.strip() for t in need("SUPABASE_TABLE").split(",") if t.strip()]:
        rows, start, page = [], 0, 1000
        while True:
            h = dict(headers, Range=f"{start}-{start + page - 1}")
            r = requests.get(f"{base}/rest/v1/{table}", headers=h,
                             params={"select": "*"}, timeout=TIMEOUT)
            r.raise_for_status()
            batch = r.json()
            rows += batch
            if len(batch) < page:
                break
            start += page
        real = [x for x in rows if not _is_test(x)]
        print(f"  {table}: {len(real)} kept, {len(rows) - len(real)} test rows skipped")
        cat = re.sub(r"_survey_\d+$", "", table)
        for i, x in enumerate(real):
            rv = _af_row(i, x)
            rv["id"] = f"appfolio:{table}:{x.get('id', i)}"
            rv["category"] = cat
            out.append(rv)
    return out


# ------------------------------------------------------------------ main
def inject(html_path, payload):
    p = Path(html_path)
    html = p.read_text(encoding="utf-8")
    block = f"{START}{json.dumps(payload, ensure_ascii=False)}{END}"
    if START in html:
        html = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, html, flags=re.S)
    else:
        if "</head>" not in html:
            sys.exit("No markers and no </head> in HTML; cannot inject.")
        tag = f'<script>window.REVIEWS_DATA = {block};</script>\n'
        html = html.replace("</head>", tag + "</head>", 1)
    p.write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="google,yelp,appfolio")
    ap.add_argument("--out", default="reviews.json")
    ap.add_argument("--html", help="exported dashboard HTML to update in place")
    ap.add_argument("--appfolio-csv")
    ap.add_argument("--find-place", metavar="NAME", help="look up Google IDs for a business name")
    a = ap.parse_args()
    load_env()
    if a.find_place:
        return find_place(a.find_place)
    use_serp = bool(os.environ.get("SERPAPI_KEY"))
    fetchers = {"google": fetch_google_serpapi if use_serp else fetch_google, "yelp": fetch_yelp,
                "appfolio": (fetch_supabase if os.environ.get("SUPABASE_URL") and not a.appfolio_csv
                     else lambda: fetch_appfolio(a.appfolio_csv))}
    reviews, errors = [], {}
    for s in [x.strip() for x in a.sources.split(",") if x.strip()]:
        try:
            got = fetchers[s]()
            print(f"{s}: {len(got)} reviews")
            reviews += got
        except Exception as e:  # keep going so one broken source doesn't block the rest
            errors[s] = str(e)
            print(f"{s}: FAILED - {e}", file=sys.stderr)
    reviews.sort(key=lambda r: r["date"] or "", reverse=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "business": "Zuma Housing", "reviews": reviews, "errors": errors}
    Path(a.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"wrote {a.out} ({len(reviews)} reviews)")
    if a.html:
        inject(a.html, payload)
        print(f"updated {a.html}")
    return 1 if errors and not reviews else 0


if __name__ == "__main__":
    sys.exit(main())
