import type { LeadFilters, RezoningLeadDetail, SortOrder } from "@/types";

export const DEFAULT_FILTERS: LeadFilters = {
  selectedCities: [],
  selectedLeadTypes: [],
  selectedScores: [],
  dateRange: { start: null, end: null },
  sortOrder: "recent",
};

/** Toggles `value` in `current`, returning a new array either way (never
 * mutates) — the standard checkbox-group update used by every filter
 * section in FilterSidebar. */
export function toggleValue<T>(current: T[], value: T): T[] {
  return current.includes(value)
    ? current.filter((existing) => existing !== value)
    : [...current, value];
}

/**
 * Applies the sidebar's filter state to a list of leads. An empty
 * selection array for a given facet (selectedCities, selectedLeadTypes,
 * selectedScores) means "no filter" — matches everything — rather than
 * "match nothing", so the default (nothing checked) shows the full table.
 *
 * Stub: this runs entirely client-side over whatever GET /api/v1/leads
 * already returned. Once the leads table is large enough that shipping
 * every row up front stops being practical, this same filter shape
 * (jurisdiction/lead_type/signal_strength/date range) should move into
 * the actual Supabase query in routers/leads.py as .in_()/.gte()/.lte()
 * clauses instead of filtering an array we already downloaded in full.
 */
export function applyFilters(
  leads: RezoningLeadDetail[],
  filters: LeadFilters,
): RezoningLeadDetail[] {
  return leads.filter((lead) => {
    if (
      filters.selectedCities.length > 0 &&
      !filters.selectedCities.includes(lead.jurisdiction)
    ) {
      return false;
    }
    if (
      filters.selectedLeadTypes.length > 0 &&
      !filters.selectedLeadTypes.includes(lead.lead_type)
    ) {
      return false;
    }
    if (
      filters.selectedScores.length > 0 &&
      !filters.selectedScores.includes(lead.signal_strength)
    ) {
      return false;
    }
    if (filters.dateRange.start && lead.meeting_date) {
      if (lead.meeting_date < filters.dateRange.start) return false;
    }
    if (filters.dateRange.end && lead.meeting_date) {
      if (lead.meeting_date > filters.dateRange.end) return false;
    }
    return true;
  });
}

export function applySort(
  leads: RezoningLeadDetail[],
  sortOrder: SortOrder,
): RezoningLeadDetail[] {
  const sorted = [...leads];
  if (sortOrder === "yield") {
    // A POLICY_AMENDMENT lead has no parcel/unit count, so it correctly
    // sorts to the bottom here — it has no yield figure to rank by.
    sorted.sort((a, b) => (b.parcel?.max_units ?? 0) - (a.parcel?.max_units ?? 0));
  } else {
    sorted.sort((a, b) => (b.meeting_date ?? "").localeCompare(a.meeting_date ?? ""));
  }
  return sorted;
}

/** Unique jurisdictions actually present in `leads`, sorted alphabetically
 * — used to populate the sidebar's city checkbox list so it only ever
 * offers cities the current data actually has, never a stale/irrelevant
 * one from the full 21-jurisdiction scraper config. */
export function uniqueJurisdictions(leads: RezoningLeadDetail[]): string[] {
  return Array.from(new Set(leads.map((lead) => lead.jurisdiction))).sort();
}
