-- Local Municipal & Zoning Intelligence Engine
-- Supabase (PostgreSQL + PostGIS) schema
-- San Mateo & Bay Area Focus

create extension if not exists postgis;
create extension if not exists "uuid-ossp";

-- Signal strength used to rank rezoning_leads for off-market development potential.
-- EXCLUDED marks a parcel disqualified by nearby exclusion language (denied,
-- historic resource, etc.) rather than a live lead.
create type signal_strength as enum ('HIGH', 'MED', 'LOW', 'EXCLUDED');
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
  file_url text not null,
  processed_status document_processed_status not null default 'PENDING',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index documents_city_name_idx on documents (city_name);
create index documents_processed_status_idx on documents (processed_status);

-- ---------------------------------------------------------------------------
-- rezoning_leads: extracted off-market development signals linking a parcel
-- to the source document it was found in
-- ---------------------------------------------------------------------------
create table rezoning_leads (
  id uuid primary key default uuid_generate_v4(),
  parcel_id uuid not null references parcels (id) on delete cascade,
  document_id uuid not null references documents (id) on delete cascade,
  signal_strength signal_strength not null,
  summary text not null,
  extracted_text_snippet text,
  created_at timestamptz not null default now()
);

create index rezoning_leads_parcel_id_idx on rezoning_leads (parcel_id);
create index rezoning_leads_document_id_idx on rezoning_leads (document_id);
create index rezoning_leads_signal_strength_idx on rezoning_leads (signal_strength);
