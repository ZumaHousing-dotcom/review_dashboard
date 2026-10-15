#!/usr/bin/env python3
"""Assign Respond.io contacts to agents based on a Google Sheet.

For every row in the sheet:
  1. look the contact up in Respond.io,
  2. skip it if it already has an assignee,
  3. otherwise assign it to the agent named in the sheet.

Leads come from a Google Sheet, the Supabase leads table, or both. Each lead needs:
    contact   phone (+15551234567), email, or Respond.io contact id
    assignee  agent email (or numeric Respond.io user id)
Sheet: header row required, names are case-insensitive.
Supabase: column names are set with SUPABASE_LEADS_* below.

Usage:
    python respondio_assign.py --tables               # list Supabase tables/columns
    python respondio_assign.py --dry-run              # show what would happen
    python respondio_assign.py                        # do it
    python respondio_assign.py --source supabase      # sheet | supabase | both (default both
                                                      # for whichever sources are configured)

Configuration (environment variables / .env):
    RESPONDIO_API_TOKEN   Respond.io > Settings > Integrations > Developer API
    SHEET_CSV_URL         Google Sheet > File > Share > Publish to web > CSV link
                          (or any "export?format=csv&gid=..." URL you can reach)
    SUPABASE_URL, SUPABASE_KEY     same ones reviews_sync.py uses
    SUPABASE_LEADS_TABLE           e.g. leads
    SUPABASE_LEADS_CONTACT_COL     column with phone/email/id   (default: phone)
    SUPABASE_LEADS_ASSIGNEE_COL    column with the agent        (default: assignee)
    SUPABASE_LEADS_FILTER          optional PostgREST filter, e.g. status=eq.new
"""
import argparse
import csv
import io
import os
import re
import sys
import time
from pathlib import Path

import requests

API = "https://api.respond.io/v2"
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
        sys.exit(f"Missing {name} (set it in the environment or .env)")
    return v


def read_sheet(url):
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    rows = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))
    for row in rows:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if row.get("contact") and row.get("assignee"):
            yield row["contact"], row["assignee"]


def _sb_headers():
    key = need("SUPABASE_KEY")
    h = {"apikey": key}
    if key.startswith("eyJ"):
        h["Authorization"] = f"Bearer {key}"
    return h


def read_supabase():
    base = need("SUPABASE_URL").rstrip("/")
    table = need("SUPABASE_LEADS_TABLE")
    c_col = os.environ.get("SUPABASE_LEADS_CONTACT_COL", "phone")
    a_col = os.environ.get("SUPABASE_LEADS_ASSIGNEE_COL", "assignee")
    params = [("select", f"{c_col},{a_col}")]
    flt = os.environ.get("SUPABASE_LEADS_FILTER", "")
    params += [tuple(f.split("=", 1)) for f in flt.split("&") if "=" in f]
    offset = 0
    while True:  # page through PostgREST's 1000-row cap
        r = requests.get(f"{base}/rest/v1/{table}", params=params, timeout=TIMEOUT,
                         headers={**_sb_headers(), "Range": f"{offset}-{offset + 999}"})
        if not r.ok:
            sys.exit(f"Supabase said {r.status_code} for '{table}': {r.text[:300]}")
        rows = r.json()
        for row in rows:
            contact, agent = str(row.get(c_col) or "").strip(), str(row.get(a_col) or "").strip()
            if contact and agent:
                yield contact, agent
        if len(rows) < 1000:
            return
        offset += 1000


def list_tables():
    base = need("SUPABASE_URL").rstrip("/")
    r = requests.get(f"{base}/rest/v1/", headers=_sb_headers(), timeout=TIMEOUT)
    if not r.ok:
        sys.exit(f"Supabase said {r.status_code}: {r.text[:300]}")
    for name, d in sorted(r.json().get("definitions", {}).items()):
        print(f"{name}: " + ", ".join(d.get("properties") or {}))


def identifier(contact):
    """Respond.io wants id:123, email:a@b.com or phone:+1555..."""
    if re.fullmatch(r"\d{1,12}", contact):
        return f"id:{contact}"
    if "@" in contact:
        return f"email:{contact}"
    return "phone:+" + re.sub(r"\D", "", contact)


class Respond:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def call(self, method, path, **kw):
        for attempt in range(5):
            r = self.s.request(method, API + path, timeout=TIMEOUT, **kw)
            if r.status_code == 429:  # rate limited
                time.sleep(int(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            return r
        return r

    def get_contact(self, ident):
        r = self.call("GET", f"/contact/{ident}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def assign(self, ident, agent):
        r = self.call("POST", f"/contact/{ident}/conversation/assignee",
                      json={"assignee": agent})
        r.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tables", action="store_true", help="list Supabase tables and columns")
    ap.add_argument("--source", choices=["sheet", "supabase", "both"], default="both")
    args = ap.parse_args()
    load_env()
    if args.tables:
        return list_tables()
    client = Respond(need("RESPONDIO_API_TOKEN"))
    counts = {"assigned": 0, "already assigned": 0, "not found": 0, "failed": 0}

    leads, seen = [], set()
    if args.source in ("sheet", "both") and (args.source == "sheet" or os.environ.get("SHEET_CSV_URL")):
        leads += read_sheet(need("SHEET_CSV_URL"))
    if args.source in ("supabase", "both") and (args.source == "supabase" or os.environ.get("SUPABASE_LEADS_TABLE")):
        leads += read_supabase()
    if not leads:
        sys.exit("No leads found (is SHEET_CSV_URL or SUPABASE_LEADS_TABLE set?)")

    for contact, agent in leads:
        ident = identifier(contact)
        if ident in seen:  # same lead in both sources: first one wins
            continue
        seen.add(ident)
        try:
            data = client.get_contact(ident)
            if data is None:
                print(f"NOT FOUND   {contact}")
                counts["not found"] += 1
            elif data.get("assignee"):
                a = data["assignee"]
                who = a.get("email") if isinstance(a, dict) else a
                print(f"SKIP        {contact} (already assigned to {who})")
                counts["already assigned"] += 1
            elif args.dry_run:
                print(f"WOULD ASSIGN {contact} -> {agent}")
            else:
                client.assign(ident, int(agent) if agent.isdigit() else agent)
                print(f"ASSIGNED    {contact} -> {agent}")
                counts["assigned"] += 1
        except requests.RequestException as e:
            print(f"FAILED      {contact}: {e}")
            counts["failed"] += 1

    print("\n" + ", ".join(f"{k}: {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
