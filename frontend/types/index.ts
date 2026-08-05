// Mirrors backend/models/schemas.py — keep in sync with the FastAPI Pydantic models.

export type SignalStrength = "HIGH" | "MED" | "LOW" | "EXCLUDED";

// SITE_SPECIFIC: a rezone/permit/variance tied to one parcel (parcel set).
// POLICY_AMENDMENT: a citywide/district-wide zoning change with no single
// subject parcel (parcel is null; affected_districts describes the scope).
export type LeadType = "SITE_SPECIFIC" | "POLICY_AMENDMENT";

export interface Parcel {
  id: string;
  apn: string;
  address: string;
  city: string;
  county: string;
  current_zoning: string | null;
  proposed_zoning: string | null;
  max_far: number | null;
  max_units: number | null;
  owner_name: string | null;
  owner_address: string | null;
  created_at: string;
  updated_at: string;
}

export interface RezoningLeadDetail {
  id: string;
  document_id: string;
  lead_type: LeadType;
  signal_strength: SignalStrength;
  summary: string;
  extracted_text_snippet: string | null;
  entitlement_type: string | null;
  // Only populated for a POLICY_AMENDMENT lead.
  affected_districts: string[] | null;
  meeting_date: string | null;
  created_at: string;
  // One or more parcels this lead is linked to (via the backend's
  // lead_parcels junction table) — a development can span an assemblage
  // of adjacent lots, so this is never assumed to have exactly one entry.
  // Empty for a POLICY_AMENDMENT lead — see LeadType. Always check length
  // before reading parcel-derived fields (address, apn, zoning, etc.).
  parcels: Parcel[];
  agenda_source_url: string;
  // The source document's own city — always present, regardless of
  // lead_type. Use this for display/filtering instead of a parcel's own
  // city, which no POLICY_AMENDMENT lead has any of.
  jurisdiction: string;
  // The source PDF page this lead was found on. Null for a lead inserted
  // before this column existed — the drawer's source-document link falls
  // back to a plain "View Agenda PDF" with no page suffix in that case.
  page_number: number | null;
}

// SORT_RECENT: newest meeting_date first. SORT_YIELD: highest
// parcels[0].max_units first — a POLICY_AMENDMENT lead has no parcels/unit
// count, so it naturally sorts to the bottom under this order, which is
// the correct behavior (it has no yield figure to rank by).
export type SortOrder = "recent" | "yield";

export interface LeadFilters {
  selectedCities: string[];
  selectedLeadTypes: LeadType[];
  selectedScores: SignalStrength[];
  dateRange: { start: string | null; end: string | null };
  sortOrder: SortOrder;
}

// Mirrors backend/services/scraper.py's MunicipalSource.
export interface MunicipalSource {
  name: string;
  city: string;
  listing_url: string;
  type: string;
}
