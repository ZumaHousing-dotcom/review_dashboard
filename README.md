# Zuma Review Dashboard Sync

`reviews_sync.py` pulls reviews from Google Business Profile, Yelp and AppFolio surveys,
normalizes them (`id, source, author, rating, text, date, url, reply`), writes `reviews.json`,
and optionally injects the data into the exported `Review Insights v2.dc.html`.

    pip install -r requirements.txt
    cp .env.example .env   # fill in credentials
    python reviews_sync.py --html "Review Insights v2.dc.html"

The HTML gets `window.REVIEWS_DATA = /*REVIEWS_DATA_START*/{...}/*REVIEWS_DATA_END*/`;
re-runs replace that block. The design must read `window.REVIEWS_DATA.reviews`.

Limits: Google needs approved Business Profile API access; Yelp's API returns only 3 excerpts
per business; AppFolio has no public review API, so use a survey CSV export (`--appfolio-csv`).


## Feeding the live dashboard (Supabase)

The dashboard (`dashboard.html`, patched by `patch_dashboard.py`) reads from Supabase every time it opens:
Google reviews from `ri_reviews` and AppFolio survey answers from the `ri_appfolio_feed` view.

1. **One time:** run `supabase/ri_appfolio_feed.sql` in the Supabase SQL Editor (anonymized view: no resident names).
2. Refresh the data and add new Google reviews to `ri_reviews`:

       python reviews_sync.py --sources google
       python reviews_sync.py --push-google --dry-run   # shows what would be added, writes nothing
       python reviews_sync.py --push-google             # adds the missing ones (never edits or deletes)

3. Deploy the updated `dashboard.html` to Netlify (as `index.html`).

Helper commands: `--tables`, `--inspect`, `--questions`, `--summary`, `--check`.
