-- Migrates rezoning_leads -> parcels from a single parcel_id FK to a real
-- many-to-many relationship via a lead_parcels junction table. Fixes real
-- data corruption: a development spanning multiple parcels (e.g. an
-- assemblage of adjacent lots, or a JIT address lookup that matches
-- several distinct APNs sharing one situs address — both confirmed live
-- this session) was previously forced onto a single parcel_id, silently
-- dropping every parcel but one.
--
-- Run this in the Supabase SQL Editor.
--
-- NOTE on dedup: rezoning_leads_parcel_meeting_date_key (added in
-- migration 0003 to dedupe a SITE_SPECIFIC lead re-encountered across two
-- different documents for the same parcel+meeting) is dropped along with
-- parcel_id below, since it was defined on that column, and there is no
-- direct replacement here — a lead's identity is no longer a single
-- (parcel, date) pair now that it can span several parcels. Duplicate
-- leads within one document's own re-ingest are still prevented (see
-- backend/routers/ingest.py, which deletes a document's prior leads
-- before reinserting), but two different documents describing the same
-- multi-parcel item could once again produce two separate lead rows
-- until a proper multi-parcel dedup key is designed.

create table lead_parcels (
  lead_id uuid not null references rezoning_leads (id) on delete cascade,
  parcel_id uuid not null references parcels (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (lead_id, parcel_id)
);

create index lead_parcels_lead_id_idx on lead_parcels (lead_id);
create index lead_parcels_parcel_id_idx on lead_parcels (parcel_id);

-- Backfill every existing lead's single parcel_id into the new junction
-- table before the column disappears, so no already-ingested SITE_SPECIFIC
-- lead silently loses its only parcel association.
insert into lead_parcels (lead_id, parcel_id)
select id, parcel_id from rezoning_leads where parcel_id is not null;

alter table rezoning_leads drop column parcel_id;
