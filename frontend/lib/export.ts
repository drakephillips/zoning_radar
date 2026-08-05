import type { RezoningLeadDetail } from "@/types";

const CSV_COLUMNS = [
  "Meeting Date",
  "Jurisdiction",
  "Lead Type",
  "Site Address",
  "APN",
  "Entitlement Type",
  "Current Zoning",
  "Proposed Zoning",
  "Max Unit Yield",
  "Signal Score",
] as const;

/** Quotes a field only when it needs it (contains a comma, quote, or
 * newline), doubling any embedded quotes per RFC 4180. */
function escapeCsvField(value: string): string {
  if (/[",\n]/.test(value)) {
    return `"${value.replaceAll('"', '""')}"`;
  }
  return value;
}

// Site Address/APN/zoning columns join every parcel a lead is linked to
// with "; " — unlike the dashboard table's truncated "+N more" display,
// a CSV export shouldn't silently drop the 2nd/3rd parcel of a
// multi-parcel assemblage.
function leadToRow(lead: RezoningLeadDetail): string[] {
  const primaryParcel = lead.parcels[0] ?? null;
  return [
    lead.meeting_date ?? "",
    lead.jurisdiction,
    lead.lead_type,
    lead.parcels.map((p) => p.address).join("; "),
    lead.parcels.map((p) => p.apn).join("; "),
    lead.entitlement_type ?? "",
    lead.parcels.map((p) => p.current_zoning ?? "").join("; "),
    lead.parcels.map((p) => p.proposed_zoning ?? "").join("; "),
    // Every parcel a lead is linked to carries the same signal-level unit
    // count (see backend/routers/ingest.py) — the primary parcel's value
    // is representative, not a sum (summing would double-count).
    primaryParcel?.max_units != null ? String(primaryParcel.max_units) : "",
    lead.signal_strength,
  ];
}

/**
 * Builds a CSV of the given leads and triggers a browser download of
 * "zoning_leads_export.csv". Callers should pass whatever slice of leads is
 * currently visible (e.g. post-filter) rather than the full unfiltered set.
 */
export function exportLeadsToCSV(leads: RezoningLeadDetail[]): void {
  const rows = [CSV_COLUMNS, ...leads.map(leadToRow)];
  const csv = rows.map((row) => row.map(escapeCsvField).join(",")).join("\n");

  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = url;
  link.download = "zoning_leads_export.csv";
  link.click();
  URL.revokeObjectURL(url);
}
