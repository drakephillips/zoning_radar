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

function leadToRow(lead: RezoningLeadDetail): string[] {
  return [
    lead.meeting_date ?? "",
    lead.jurisdiction,
    lead.lead_type,
    lead.parcel?.address ?? "",
    lead.parcel?.apn ?? "",
    lead.entitlement_type ?? "",
    lead.parcel?.current_zoning ?? "",
    lead.parcel?.proposed_zoning ?? "",
    lead.parcel?.max_units != null ? String(lead.parcel.max_units) : "",
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
