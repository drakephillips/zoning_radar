-- Adds a UNIQUE(parcel_id, meeting_date) constraint to rezoning_leads so a
-- SITE_SPECIFIC lead for the same parcel on the same meeting date can never
-- be inserted twice (see backend/routers/ingest.py's _persist_to_supabase,
-- which now upserts on this exact pair instead of always inserting). Fixes
-- duplicate rows produced when two documents describe the same item (e.g.
-- an Agenda PDF and a later Minutes PDF for the same meeting, or a
-- re-scrape of the same packet under a slightly different URL).
--
-- POLICY_AMENDMENT leads are never affected: parcel_id is always null for
-- them (see lead_type), and Postgres treats every NULL as distinct from
-- every other NULL, so the constraint below never restricts those rows.
--
-- Run this in the Supabase SQL Editor. Safe to run against a table that
-- already has duplicate rows: the cleanup step below removes them first,
-- keeping the most recently inserted row of each (parcel_id, meeting_date)
-- pair — the constraint would otherwise reject outright on a table with
-- existing violations.

delete from rezoning_leads a
using rezoning_leads b
where a.parcel_id is not null
  and a.parcel_id = b.parcel_id
  and a.meeting_date = b.meeting_date
  and a.created_at < b.created_at;

alter table rezoning_leads
  add constraint rezoning_leads_parcel_meeting_date_key
  unique (parcel_id, meeting_date);
