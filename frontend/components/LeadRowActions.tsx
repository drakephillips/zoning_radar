"use client";

import { Download, FileText } from "lucide-react";
import type { Parcel, RezoningLeadDetail } from "@/types";

/** One row per linked parcel — a lead can span an assemblage of several
 * parcels (see backend's lead_parcels junction table), each with its own
 * owner, so exporting only the first would silently drop the rest. */
function exportOwnerInfo(parcels: Parcel[]) {
  const rows = [
    ["APN", "Address", "Owner Name", "Owner Address"],
    ...parcels.map((parcel) => [
      parcel.apn,
      parcel.address,
      parcel.owner_name ?? "",
      parcel.owner_address ?? "",
    ]),
  ];
  const csv = rows.map((row) => row.map((cell) => `"${cell}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = url;
  link.download = `owner-${parcels[0].apn}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

export default function LeadRowActions({ lead }: { lead: RezoningLeadDetail }) {
  return (
    <div className="flex items-center justify-end gap-1">
      <a
        href={lead.agenda_source_url}
        target="_blank"
        rel="noopener noreferrer"
        title="View Agenda PDF"
        aria-label="View Agenda PDF"
        className="inline-flex h-7 w-7 items-center justify-center rounded-sm border border-slate-700 text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200"
      >
        <FileText className="h-3.5 w-3.5" />
      </a>
      {/* A policy amendment has no parcels/owners to export — see
          LeadType. Only a SITE_SPECIFIC lead has any linked parcels. */}
      {lead.parcels.length > 0 && (
        <button
          type="button"
          onClick={() => exportOwnerInfo(lead.parcels)}
          title="Export Owner Info"
          aria-label="Export Owner Info"
          className="inline-flex h-7 w-7 items-center justify-center rounded-sm border border-slate-700 text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200"
        >
          <Download className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
