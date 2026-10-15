#!/usr/bin/env python3
"""Assign Respond.io contacts to agents based on a Google Sheet.

For every row in the sheet:
  1. look the contact up in Respond.io,
  2. skip it if it already has an assignee,
  3. otherwise assign it to the agent named in the sheet.

Sheet columns (header row required, names are case-insensitive):
    contact   phone (+15551234567), email, or Respond.io contact id
    assignee  agent email (or numeric Respond.io user id)

Usage:
    python respondio_assign.py --dry-run     # show what would happen
    python respondio_assign.py               # do it

Configuration (environment variables / .env):
    RESPONDIO_API_TOKEN   Respond.io > Settings > Integrations > Developer API
    SHEET_CSV_URL         Google Sheet > File > Share > Publish to web > CSV link
                          (or any "export?format=csv&gid=..." URL you can reach)
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
    args = ap.parse_args()
    load_env()
    client = Respond(need("RESPONDIO_API_TOKEN"))
    counts = {"assigned": 0, "already assigned": 0, "not found": 0, "failed": 0}

    for contact, agent in read_sheet(need("SHEET_CSV_URL")):
        ident = identifier(contact)
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
