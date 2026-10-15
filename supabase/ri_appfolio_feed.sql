-- Read-only feed of AppFolio survey answers for the Review Insights dashboard.
-- Safe to run: it only creates (or replaces) this one view. It does not change any table.
--
-- Privacy: the dashboard's Supabase key is visible to anyone who opens the page, so this view
-- leaves out the resident's name and the vendor. Each survey gets an anonymous response_key
-- (a hash of its survey_id) so the dashboard can group one resident's answers together.
-- Still included: survey type, property, date, assigned staff member, the question and the answer.
-- Free-text answers are what residents typed, so they can mention names.

create or replace view public.ri_appfolio_feed as
select
  md5(coalesce(s.survey_type::text, '') || '|' || coalesce(s.survey_id::text, '')) as response_key,
  s.survey_type,
  s.property,
  s.date_received,
  s.assigned_user,
  q.question_no,
  q.item->>'id'    as question,
  q.item->>'value' as answer
from appfolio.survey_responses s
cross join lateral jsonb_array_elements(s.questions) with ordinality as q(item, question_no)
where q.item->>'value' is not null
  and btrim(q.item->>'value') <> '';

grant select on public.ri_appfolio_feed to anon, authenticated;
