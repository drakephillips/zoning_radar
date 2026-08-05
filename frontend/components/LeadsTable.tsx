"use client";

import { useState } from "react";
import type { RezoningLeadDetail } from "@/types";
import { formatTimestamp } from "@/lib/format";
import SignalBadge from "./SignalBadge";
import LeadRowActions from "./LeadRowActions";
import DealSheetDrawer from "./DealSheetDrawer";

export default function LeadsTable({
  leads,
  emptyMessage,
}: {
  leads: RezoningLeadDetail[];
  /** Overrides the empty-state copy — e.g. Dashboard passes a
   * "no leads match your filters" message when the base data isn't
   * actually empty, filtering just hid everything. */
  emptyMessage?: string;
}) {
  const [selectedLead, setSelectedLead] = useState<RezoningLeadDetail | null>(null);

  if (leads.length === 0) {
    return (
      <div className="rounded-sm border border-slate-800 bg-slate-900/60 p-10 text-center text-sm text-slate-500">
        {emptyMessage ??
          "No active market signals in local cache. Select target markets and click 'Run Market Scan' to execute a sweep."}
      </div>
    );
  }

  return (
    <div className="overflow-x-hidden rounded-sm border border-slate-800 bg-slate-900/60">
      <table className="w-full table-fixed text-left font-mono text-sm">
        <colgroup>
          <col className="w-[12%]" />
          <col className="w-[13%]" />
          <col className="w-[24%]" />
          <col className="w-[27%]" />
          <col className="w-[8%]" />
          <col className="w-[8%]" />
          <col className="w-[8%]" />
        </colgroup>
        <thead>
          <tr className="border-b border-slate-800 text-[11px] uppercase tracking-wide text-slate-500">
            <th className="px-3 py-2 font-medium">Meeting Date</th>
            <th className="px-3 py-2 font-medium">Jurisdiction</th>
            <th className="px-3 py-2 font-medium">Site Address</th>
            <th className="px-3 py-2 font-medium">Entitlement Type</th>
            <th className="px-3 py-2 text-right font-medium">Unit Yield</th>
            <th className="px-3 py-2 text-center font-medium">Signal Score</th>
            <th className="px-3 py-2 text-right font-medium">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/60">
          {leads.map((lead) => {
            const isSelected = selectedLead?.id === lead.id;
            return (
              <tr
                key={lead.id}
                onClick={() => setSelectedLead(lead)}
                className={`cursor-pointer transition-colors ${
                  isSelected ? "bg-slate-800/70" : "hover:bg-slate-800/40"
                }`}
              >
                <td
                  className="truncate px-3 py-2 text-slate-300"
                  title={`Extracted ${formatTimestamp(lead.created_at)}`}
                >
                  {lead.meeting_date ?? "—"}
                </td>
                <td className="truncate px-3 py-2 text-slate-300">{lead.jurisdiction}</td>
                <td className="px-3 py-2 text-slate-300">
                  {lead.parcels.length > 0 ? (
                    <>
                      <span className="flex items-center gap-1.5">
                        <span className="truncate">{lead.parcels[0].address}</span>
                        {lead.parcels.length > 1 && (
                          <span
                            className="shrink-0 rounded-sm bg-slate-800 px-1.5 py-0.5 font-sans text-[10px] font-medium text-slate-400"
                            title={lead.parcels
                              .slice(1)
                              .map((p) => p.address)
                              .join(", ")}
                          >
                            +{lead.parcels.length - 1} more
                          </span>
                        )}
                      </span>
                      <span className="block truncate text-xs text-slate-600">
                        {lead.parcels[0].apn}
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="block truncate font-sans italic text-slate-400">
                        Policy Amendment
                      </span>
                      <span className="block truncate text-xs text-slate-600">
                        {lead.affected_districts && lead.affected_districts.length > 0
                          ? lead.affected_districts.join(", ")
                          : "Citywide"}
                      </span>
                    </>
                  )}
                </td>
                <td className="truncate px-3 py-2 text-slate-300">
                  {lead.entitlement_type ?? "—"}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                  {lead.parcels[0] && lead.parcels[0].max_units !== null
                    ? `+${lead.parcels[0].max_units}`
                    : "—"}
                </td>
                <td className="px-3 py-2 font-sans">
                  <div className="flex justify-center">
                    <SignalBadge strength={lead.signal_strength} />
                  </div>
                </td>
                <td className="px-3 py-2 font-sans" onClick={(e) => e.stopPropagation()}>
                  <div className="flex justify-end">
                    <LeadRowActions lead={lead} />
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {selectedLead && (
        <DealSheetDrawer lead={selectedLead} onClose={() => setSelectedLead(null)} />
      )}
    </div>
  );
}
