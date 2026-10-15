-- Read-only feed of AppFolio survey answers for the Review Insights dashboard.
-- Safe to run: it only creates a new view. It does not change appfolio_reviews or any other table.
--
-- Privacy: the dashboard's Supabase key is visible to anyone who opens the page, so this view
-- leaves out the resident's name and the vendor. Each survey response gets an anonymous
-- response_key (a hash) so the dashboard can group a resident's answers together.
-- Still included: survey type, property, date, assigned staff member, the question and the answer.
-- Free-text answers are what residents typed, so they can mention names.

create or replace view public.ri_appfolio_feed as
select
  md5(
    coalesce(survey_type::text, '') || '|' || coalesce(tenant::text, '') || '|' ||
    coalesce(property::text, '')    || '|' || coalesce(date_sent::text, '') || '|' ||
    coalesce(date_received::text, '')
  )                       as response_key,
  survey_type,
  property,
  date_received,
  assigned_user,
  question_no,
  question,
  answer
from public.appfolio_reviews
where answer is not null
  and btrim(answer::text) <> '';

grant select on public.ri_appfolio_feed to anon, authenticated;
