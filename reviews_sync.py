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


def _pick_date(row):
    """First value that looks like a date (2026-01-31...), ignoring yes/no columns."""
    low = {k.strip().lower(): v for k, v in row.items() if k}
    names = ["reviewed_at", "submitted_at", "completed_at", "created_at", "submitted", "date", "completed", "created"]
    names += [k for k in low if k.endswith("_at") or "date" in k]
    for n in names:
        v = low.get(n)
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
            return v
    return None


def _af_row(i, row):
    rating = _pick(row, "rating", "score", "overall rating", "overall", "stars", "overall_rating", "overall_score", "satisfaction", "nps")
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        rating = None
    return review("appfolio", _pick(row, "id", "survey id") or i,
                  _pick(row, "name", "resident", "tenant", "author"), rating,
                  _pick(row, "comment", "comments", "review", "feedback", "text", "body", "additional_comments", "open_response"),
                  _pick_date(row))


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


def load_appfolio_csv(path):
    """Upload an AppFolio report download into the appfolio_reviews table. Re-running with an
    overlapping file is safe: rows are matched on external_id and updated, not duplicated."""
    import hashlib
    base = need("SUPABASE_URL").rstrip("/")
    key = need("SUPABASE_KEY")
    headers = {"apikey": key, "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    text = Path(path).read_text(encoding="utf-8-sig")
    rows = []
    for i, raw in enumerate(csv.DictReader(text.splitlines())):
        rv = _af_row(i, raw)
        fp = "|".join(str(rv[k] or "") for k in ("author", "date", "text", "rating"))
        ext = _pick(raw, "id", "survey id", "response id") or hashlib.sha1(fp.encode()).hexdigest()
        rows.append({"external_id": str(ext), "author": rv["author"], "rating": rv["rating"],
                     "body": rv["text"], "reviewed_at": rv["date"], "raw": raw,
                     "category": _pick(raw, "survey", "survey type", "type", "category"),
                     "property": _pick(raw, "property", "property name")})
    rows = list({r["external_id"]: r for r in rows}.values())  # one row per ID within a single upload
    for n in range(0, len(rows), 500):
        r = requests.post(f"{base}/rest/v1/appfolio_reviews", params={"on_conflict": "external_id"},
                          headers=headers, data=json.dumps(rows[n:n + 500]), timeout=TIMEOUT)
        if not r.ok:
            sys.exit(f"Supabase said {r.status_code}: {r.text[:300]}")
    print(f"loaded {len(rows)} rows into appfolio_reviews")


def check_json(path):
    """Print counts only (no names or text) so the loaded data can be sanity-checked."""
    from collections import Counter
    from datetime import timedelta
    data = json.loads(Path(path).read_text())["reviews"]
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    for src in sorted({r["source"] for r in data}):
        rs = [r for r in data if r["source"] == src]
        dates = sorted((r["date"] or "")[:10] for r in rs if r["date"])
        print(f"\n{src}: {len(rs)} reviews")
        print(f"  dates: {dates[0]} to {dates[-1]}" if dates else "  dates: none found")
        print(f"  no date: {sum(1 for r in rs if not r['date'])}, no rating: {sum(1 for r in rs if r['rating'] is None)}, "
              f"no text: {sum(1 for r in rs if not r['text'])}")
        print("  ratings:", dict(sorted(Counter(r["rating"] for r in rs).items(), key=lambda kv: str(kv[0]))))
        print(f"  since Monday {monday}: {sum(1 for r in rs if (r['date'] or '')[:10] >= str(monday))}, "
              f"last 7 days: {sum(1 for r in rs if (r['date'] or '')[:10] >= str(today - timedelta(days=7)))}")
        by_month = Counter((r["date"] or "none")[:7] for r in rs)
        print("  by month:", dict(sorted(by_month.items())))
        print(f"  distinct authors: {len({r['author'] for r in rs})}, distinct texts: {len({r['text'] for r in rs if r['text']})}")


def list_questions(table="appfolio_reviews"):
    """List each survey question with how many answers it has and what the answers look like.
    Answers are shown only for short multiple-choice or number questions, never free text."""
    from collections import Counter, defaultdict
    base = need("SUPABASE_URL").rstrip("/")
    key = need("SUPABASE_KEY")
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    rows, start = [], 0
    while True:
        r = requests.get(f"{base}/rest/v1/{table}", timeout=TIMEOUT, params={
            "select": "survey_type,question_no,question,answer,tenant,property,date_received"},
            headers=dict(headers, Range=f"{start}-{start + 999}"))
        if not r.ok:
            sys.exit(f"Supabase said {r.status_code}: {r.text[:300]}")
        rows += r.json()
        if len(r.json()) < 1000:
            break
        start += 1000
    groups = defaultdict(list)
    for x in rows:
        groups[(x.get("survey_type"), x.get("question_no"), x.get("question"))].append(x.get("answer"))
    resp = {(x.get("survey_type"), x.get("tenant"), x.get("property"), x.get("date_received")) for x in rows}
    print(f"{len(rows)} rows = about {len(resp)} survey responses\n")
    for (st, no, q), answers in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        vals = [a for a in answers if a not in (None, "")]
        nums = sum(1 for a in vals if re.fullmatch(r"\d+(\.\d+)?", str(a).strip()))
        distinct = Counter(str(a).strip() for a in vals)
        print(f"[{st}] Q{no}: {q}")
        line = f"    {len(vals)} answered, {nums} numeric, {len(distinct)} distinct"
        if 0 < len(distinct) <= 12:
            line += "  ->  " + ", ".join(f"{k}: {n}" for k, n in sorted(distinct.items()))
        print(line)


def summarize_supabase():
    """Counts only: rows per source and date range for the review tables. No names or text."""
    from collections import Counter
    base = need("SUPABASE_URL").rstrip("/")
    key = need("SUPABASE_KEY")
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"

    def rows(table, cols):
        out, start = [], 0
        while True:
            r = requests.get(f"{base}/rest/v1/{table}", params={"select": cols}, timeout=TIMEOUT,
                             headers=dict(headers, Range=f"{start}-{start + 999}"))
            if not r.ok:
                print(f"  {table}: error {r.status_code} {r.text[:200]}")
                return None
            out += r.json()
            if len(r.json()) < 1000:
                return out
            start += 1000

    def span(vals):
        vals = sorted(v[:10] for v in vals if v)
        return f"{vals[0]} to {vals[-1]}" if vals else "no dates"

    for table, src, date in [("ri_reviews", "source", "created_at"),
                             ("google_reviews", None, "published_at"),
                             ("ri_properties", None, None)]:
        print(f"\n{table}")
        data = rows(table, ",".join(c for c in (src, date, "name" if table == "ri_properties" else None) if c) or "*")
        if data is None:
            continue
        print(f"  {len(data)} rows" + (f", dates {span([d.get(date) for d in data])}" if date else ""))
        if src:
            for k, n in Counter(d.get(src) for d in data).most_common():
                sub = [d.get(date) for d in data if d.get(src) == k]
                print(f"    {k}: {n} ({span(sub)})")
        if table == "ri_properties":
            print("  properties: " + ", ".join(sorted(str(d.get("name")) for d in data))[:600])


def list_tables():
    """Print every table in the Supabase project with its column names (no row data)."""
    base = need("SUPABASE_URL").rstrip("/")
    key = need("SUPABASE_KEY")
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    r = requests.get(f"{base}/rest/v1/", headers=headers, timeout=TIMEOUT)
    if not r.ok:
        sys.exit(f"Supabase said {r.status_code}: {r.text[:300]}")
    defs = r.json().get("definitions", {})
    print(f"{len(defs)} tables found\n")
    for name in sorted(defs):
        cols = list((defs[name].get("properties") or {}))
        print(f"{name}  ({len(cols)} columns)")
        print("   " + ", ".join(cols[:25]) + (" ..." if len(cols) > 25 else ""))


def inspect_supabase():
    """Print column names and types (never any row data) for each configured table."""
    base = need("SUPABASE_URL").rstrip("/")
    key = need("SUPABASE_KEY")
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    r = requests.get(f"{base}/rest/v1/", headers=headers, timeout=TIMEOUT)
    if not r.ok:
        sys.exit(f"Supabase said {r.status_code}: {r.text[:300]}")
    defs = r.json().get("definitions", {})
    for table in [t.strip() for t in need("SUPABASE_TABLE").split(",") if t.strip()]:
        print(f"\n{table}")
        cols = defs.get(table, {}).get("properties")
        if not cols:
            print("  (not found, or no columns visible)")
        for name, info in (cols or {}).items():
            print(f"  {name}  [{info.get('format') or info.get('type')}]")


# Extra survey questions, by survey type (rating-style first, then multiple-choice answers).
SURVEY_SCORES = {"maintenance": ["speed", "tech_rating"], "move_in": ["leasing"],
                 "move_out": ["instructions"], "showings": ["booking", "agent_rating"]}
SURVEY_ANSWERS = {"maintenance": ["fixed", "informed"], "move_in": ["ready", "source"],
                  "move_out": ["inspection", "reason", "rent_again"], "showings": ["matched", "apply"]}


def _survey_row(cat, table, x):
    """Turn one completed survey response into a review."""
    good, better = (x.get("did_well") or "").strip(), (x.get("do_better") or "").strip()
    text = "\n\n".join(p for p in (good, f"Could be better: {better}" if better else "") if p)
    name = (x.get("quote_name") or "").strip() if x.get("quote_ok") else ""
    month = next((x[k] for k in ("done_month", "move_in_month", "move_out_month", "showing_month")
                  if x.get(k)), None)
    rv = review("appfolio", f"{table}:{x.get('id')}", name or "Anonymous", x.get("overall"),
                text, x.get("created_at"), category=cat)
    rv.update({"nps": x.get("nps"), "property": x.get("property"), "agent": x.get("agent"),
               "month": month, "can_quote": bool(x.get("quote_ok")),
               "needs_recovery": bool(x.get("needs_recovery")),
               "scores": {k: x[k] for k in SURVEY_SCORES.get(cat, []) if x.get(k) is not None},
               "answers": {k: x[k] for k in SURVEY_ANSWERS.get(cat, []) if x.get(k)}})
    return rv


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
            if not r.ok:
                raise RuntimeError(f"Supabase said {r.status_code} for table '{table}': {r.text[:300]} "
                                   f"(key starts '{key[:15]}', {len(key)} characters)")
            batch = r.json()
            rows += batch
            if len(batch) < page:
                break
            start += page
        if re.search(r"_survey_\d+$", table):
            done = [x for x in rows if not x.get("is_test") and x.get("completed")]
            n_test = sum(1 for x in rows if x.get("is_test"))
            n_open = sum(1 for x in rows if not x.get("is_test") and not x.get("completed"))
            print(f"  {table}: {len(done)} kept, {n_test} test rows skipped, {n_open} unfinished skipped")
            cat = re.sub(r"_survey_\d+$", "", table)
            out += [_survey_row(cat, table, x) for x in done]
        else:  # a table loaded from an AppFolio report download
            done = [x for x in rows if not _is_test(x)]
            print(f"  {table}: {len(done)} kept, {len(rows) - len(done)} test rows skipped")
            for i, x in enumerate(done):
                rv = _af_row(i, x)
                rv["id"] = f"appfolio:{table}:{x.get('id', i)}"
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
    ap.add_argument("--load-appfolio-csv", metavar="FILE", help="upload an AppFolio report into Supabase")
    ap.add_argument("--check", metavar="FILE", nargs="?", const="reviews.json", help="print counts for a reviews.json")
    ap.add_argument("--questions", action="store_true", help="list the AppFolio survey questions")
    ap.add_argument("--summary", action="store_true", help="count rows in the review tables")
    ap.add_argument("--tables", action="store_true", help="list every Supabase table")
    ap.add_argument("--inspect", action="store_true", help="list the Supabase table columns")
    ap.add_argument("--find-place", metavar="NAME", help="look up Google IDs for a business name")
    a = ap.parse_args()
    load_env()
    if a.questions:
        return list_questions()
    if a.check:
        return check_json(a.check)
    if a.load_appfolio_csv:
        return load_appfolio_csv(a.load_appfolio_csv)
    if a.summary:
        return summarize_supabase()
    if a.tables:
        return list_tables()
    if a.inspect:
        return inspect_supabase()
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
    seen, unique = set(), []
    for r in reviews:  # weekly downloads overlap, so drop exact repeats
        k = (r["source"], r["author"], r["date"], r["text"], r["rating"])
        if r["id"] in seen or (r["text"] and k in seen):
            continue
        seen.add(r["id"]); seen.add(k)
        unique.append(r)
    if len(unique) != len(reviews):
        print(f"removed {len(reviews) - len(unique)} duplicate reviews")
    reviews = unique
    reviews.sort(key=lambda r: str(r["date"] or ""), reverse=True)
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
