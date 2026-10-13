-- New table for AppFolio survey reviews. Safe to run: it only creates something new.
create table if not exists public.appfolio_reviews (
  id          uuid primary key default gen_random_uuid(),
  external_id text not null unique,          -- the report's own ID, or a fingerprint, so re-loads don't duplicate
  author      text,
  rating      numeric,
  body        text,
  reviewed_at timestamptz,
  category    text,                          -- e.g. move_in, move_out, maintenance
  property    text,
  raw         jsonb,                         -- the original row from the report, untouched
  imported_at timestamptz not null default now()
);
create index if not exists appfolio_reviews_reviewed_at_idx on public.appfolio_reviews (reviewed_at desc);
-- Private by default: only the secret key can read or write it.
alter table public.appfolio_reviews enable row level security;
