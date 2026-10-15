-- Lets the dashboard's public key read Yelp reviews, next to the Google rule you already added.
-- Run it once in the Supabase SQL Editor. It only adds a rule; nothing is removed or changed.
-- Yelp reviews are already public on Yelp; no other kind of row becomes readable.

create policy "public can read yelp reviews"
  on public.ri_reviews for select to anon
  using (source = 'yelp');
