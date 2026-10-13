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
