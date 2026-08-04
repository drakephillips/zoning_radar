-- Adds support for macro-level policy leads (citywide/district-wide zoning
-- code amendments with no single subject parcel) alongside the existing
-- site-specific ones. Run once against the live database; database/schema.sql
-- has been updated in parallel so a fresh install matches this end state.
--
-- Safe to run against a table that already has rows: every change here is
-- additive or loosens an existing constraint (nothing is dropped, and the
-- new column gets a default), so no existing row needs to change shape.

create type lead_type as enum ('SITE_SPECIFIC', 'POLICY_AMENDMENT');

alter table rezoning_leads
  add column lead_type lead_type not null default 'SITE_SPECIFIC',
  add column affected_districts text[],
  alter column parcel_id drop not null;
