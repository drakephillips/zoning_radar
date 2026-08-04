-- Adds the source PDF page number a lead was found on, for the frontend's
-- "View Agenda PDF (Page N)" deep link in the inspection drawer. Safe to
-- run against a table that already has rows: purely additive, and the new
-- column is nullable with no NOT NULL constraint, so every existing row
-- just gets page_number = null (rendered as "page unknown" client-side)
-- until it's re-ingested.

alter table rezoning_leads
  add column page_number integer;
