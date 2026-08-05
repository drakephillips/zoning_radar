-- Local Municipal & Zoning Intelligence Engine
-- Supabase (PostgreSQL + PostGIS) schema
-- San Mateo & Bay Area Focus

create extension if not exists postgis;
create extension if not exists "uuid-ossp";

-- Signal strength used to rank rezoning_leads for off-market development potential.
-- EXCLUDED marks a parcel disqualified by nearby exclusion language (denied,
-- historic resource, etc.) rather than a live lead.
create type signal_strength as enum ('HIGH', 'MED', 'LOW', 'EXCLUDED');
-- SITE_SPECIFIC: a rezone/permit/variance tied to one or more parcels (see
-- lead_parcels below — a development can span several adjacent lots).
-- POLICY_AMENDMENT: a citywide/district-wide zoning code or General Plan
-- change with no single subject parcel (no lead_parcels rows at all;
-- affected_districts describes the change's scope instead) — e.g. a
-- Title 27 text amendment establishing new zoning districts.
create type lead_type as enum ('SITE_SPECIFIC', 'POLICY_AMENDMENT');
create type document_processed_status as enum ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED');
create type document_type as enum (
  'CITY_COUNCIL_AGENDA',
  'CITY_COUNCIL_MINUTES',
  'PLANNING_COMMISSION_AGENDA',
  'PLANNING_COMMISSION_MINUTES',
  'STAFF_REPORT',
  'GENERAL_PLAN_AMENDMENT',
  'OTHER'
);

-- ---------------------------------------------------------------------------
-- parcels: assessor-linked parcel records with current/proposed zoning state
-- ---------------------------------------------------------------------------
create table parcels (
  id uuid primary key default uuid_generate_v4(),
  apn text not null unique,
  address text not null,
  city text not null,
  county text not null default 'San Mateo',
  current_zoning text,
  proposed_zoning text,
  max_far numeric(6, 2),
  max_units integer,
  owner_name text,
  owner_address text,
  geometry geometry(Geometry, 4326),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index parcels_geometry_idx on parcels using gist (geometry);
create index parcels_apn_idx on parcels (apn);
create index parcels_city_idx on parcels (city);

-- ---------------------------------------------------------------------------
-- documents: source municipal PDFs / agendas ingested by the pipeline
-- ---------------------------------------------------------------------------
create table documents (
  id uuid primary key default uuid_generate_v4(),
  city_name text not null,
  document_type document_type not null,
  meeting_date date,
  -- Unique so a daily delta scraper run can safely upsert on file_url
  -- (see backend/routers/ingest.py's _persist_to_supabase) instead of
  -- crashing on a unique-violation when it legitimately re-encounters the
  -- same document across runs. Must be the document's real, distinct URL —
  -- never a generic display title multiple documents could share.
  file_url text not null unique,
  processed_status document_processed_status not null default 'PENDING',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index documents_city_name_idx on documents (city_name);
create index documents_processed_status_idx on documents (processed_status);
create index documents_meeting_date_idx on documents (meeting_date);

-- ---------------------------------------------------------------------------
-- rezoning_leads: extracted off-market development signals linking one or
-- more parcels (see lead_parcels below) to the source document they were
-- found in
-- ---------------------------------------------------------------------------
create table rezoning_leads (
  id uuid primary key default uuid_generate_v4(),
  document_id uuid not null references documents (id) on delete cascade,
  lead_type lead_type not null default 'SITE_SPECIFIC',
  signal_strength signal_strength not null,
  summary text not null,
  extracted_text_snippet text,
  -- Populated when the source document names a specific entitlement being
  -- sought (e.g. "General Plan Amendment", "Conditional Use Permit") — a
  -- per-lead attribute, since the same parcel can accrue leads of different
  -- entitlement types over time.
  entitlement_type text,
  -- Only populated for a POLICY_AMENDMENT lead — the district(s), plan
  -- area(s), or scope a macro zoning change affects (e.g. "Citywide" or
  -- "Downtown Precise Plan Area"). Always null for SITE_SPECIFIC.
  affected_districts text[],
  -- The city council/planning commission meeting date printed on the
  -- source agenda, as extracted by Gemini (see services/pdf_parser.py).
  -- A strict `date` column: pdf_parser.py's _GeminiLeadItem.meeting_date
  -- validator nulls out anything that isn't a real, strictly-formatted
  -- "YYYY-MM-DD" value before it ever leaves the extraction pipeline, and
  -- _persist_to_supabase falls back to the parent document's meeting_date
  -- when a lead's own is null — so nothing uncastable ever reaches here.
  meeting_date date,
  -- The source PDF page this signal was found on (see
  -- ExtractedParcelSignal.page_number), for the frontend's "View Agenda
  -- PDF (Page N)" deep link. Nullable since rows inserted before this
  -- column existed have no value for it — every new insert always sets it.
  page_number integer,
  created_at timestamptz not null default now()
);

create index rezoning_leads_document_id_idx on rezoning_leads (document_id);
create index rezoning_leads_signal_strength_idx on rezoning_leads (signal_strength);

-- ---------------------------------------------------------------------------
-- lead_parcels: many-to-many junction between rezoning_leads and parcels —
-- a SITE_SPECIFIC lead can span more than one parcel (an assemblage of
-- adjacent lots, or a JIT address lookup matching several distinct APNs
-- that share one situs address — see backend/services/parcel_resolver.py).
-- A POLICY_AMENDMENT lead has no rows here at all (see lead_type).
-- ---------------------------------------------------------------------------
create table lead_parcels (
  lead_id uuid not null references rezoning_leads (id) on delete cascade,
  parcel_id uuid not null references parcels (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (lead_id, parcel_id)
);

create index lead_parcels_lead_id_idx on lead_parcels (lead_id);
create index lead_parcels_parcel_id_idx on lead_parcels (parcel_id);
