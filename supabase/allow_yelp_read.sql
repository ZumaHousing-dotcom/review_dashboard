-- Lets the dashboard's public key read Yelp reviews as well as Google reviews.
-- Replaces the earlier Google-only rule on ri_reviews. Run it once in the Supabase SQL Editor.
-- Yelp and Google reviews are already public on those sites; no other kind of row becomes readable.

drop policy if exists "public can read google reviews" on public.ri_reviews;

create policy "public can read google and yelp reviews"
  on public.ri_reviews for select to anon
  using (source in ('google', 'yelp'));
