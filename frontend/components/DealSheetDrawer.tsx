"use client";

import { FileText, X } from "lucide-react";
import type { RezoningLeadDetail } from "@/types";
import { formatTimestamp } from "@/lib/format";
import SignalBadge from "./SignalBadge";
import ZoningChange from "./ZoningChange";

interface DealSheetDrawerProps {
  lead: RezoningLeadDetail;
  onClose: () => void;
}

export default function DealSheetDrawer({ lead, onClose }: DealSheetDrawerProps) {
  const headline = lead.parcel ? lead.parcel.address : "Policy Amendment";
  const scope =
    lead.affected_districts && lead.affected_districts.length > 0
      ? lead.affected_districts.join(", ")
      : "Citywide";
  const subheadline = lead.parcel ? `${lead.parcel.city}, ${lead.parcel.county} County` : scope;

  const pdfLabel = lead.page_number
    ? `View Agenda PDF (Page ${lead.page_number})`
    : "View Agenda PDF";

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/70" onClick={onClose} />
      {/* 30-40% of viewport width on larger screens, with a floor so it
          stays readable on a laptop-width viewport and a ceiling so it
          doesn't balloon on an ultrawide monitor. Full width on mobile. */}
      <aside className="fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-slate-800 bg-slate-950 shadow-2xl shadow-black/60 sm:w-[38vw] sm:min-w-[420px] sm:max-w-xl">
        <header className="border-b border-slate-800 px-5 py-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate font-mono text-[10px] uppercase tracking-widest text-slate-500">
                {lead.jurisdiction} · {lead.meeting_date ?? "Meeting date unknown"}
              </p>
              <h2 className="mt-0.5 truncate text-lg font-semibold text-slate-100" title={headline}>
                {headline}
              </h2>
              <p className="truncate text-xs text-slate-500">{subheadline}</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close inspection drawer"
              className="shrink-0 rounded-sm p-1.5 text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-200"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <SignalBadge strength={lead.signal_strength} />
            {lead.entitlement_type && (
              <span className="font-mono text-xs text-slate-400">{lead.entitlement_type}</span>
            )}
            <span
              className="font-mono text-xs text-slate-600"
              title="When this signal was extracted"
            >
              Extracted {formatTimestamp(lead.created_at)}
            </span>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-5">
          <section>
            <p className="text-xs uppercase tracking-wider text-slate-500">Parcel Breakdown</p>
            {lead.parcel ? (
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-slate-600">APN</dt>
                  <dd className="mt-0.5 font-mono text-slate-100">{lead.parcel.apn}</dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-wide text-slate-600">
                    Max Unit Yield
                  </dt>
                  <dd className="mt-0.5 font-mono tabular-nums text-slate-100">
                    {lead.parcel.max_units !== null ? `${lead.parcel.max_units} units` : "N/A"}
                  </dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-[11px] uppercase tracking-wide text-slate-600">
                    Zoning Change
                  </dt>
                  <dd className="mt-1">
                    <ZoningChange
                      current={lead.parcel.current_zoning}
                      proposed={lead.parcel.proposed_zoning}
                    />
                  </dd>
                </div>
              </dl>
            ) : (
              // Simplified for a POLICY_AMENDMENT lead — no single parcel,
              // so APN/zoning/unit-yield are all genuinely N/A rather than
              // just missing data.
              <div className="mt-2 rounded-sm border border-slate-800 bg-slate-900/60 p-3">
                <p className="text-sm text-slate-300">
                  N/A — a policy amendment has no single subject parcel.
                </p>
                <p className="mt-1 text-xs text-slate-500">Affected scope: {scope}</p>
              </div>
            )}
          </section>

          <section className="mt-6">
            <p className="text-xs uppercase tracking-wider text-slate-500">Verbatim Citation</p>
            {lead.extracted_text_snippet ? (
              <blockquote className="mt-2 border-l-2 border-emerald-500/50 bg-slate-900/60 py-2 pl-3 text-sm italic leading-relaxed text-slate-300">
                &ldquo;{lead.extracted_text_snippet}&rdquo;
              </blockquote>
            ) : (
              <p className="mt-2 text-sm text-slate-600">
                No source text was captured for this lead.
              </p>
            )}
          </section>

          <section className="mt-6">
            <p className="text-xs uppercase tracking-wider text-slate-500">Executive Summary</p>
            <p className="mt-2 text-sm text-slate-300">{lead.summary}</p>
          </section>

          <a
            href={lead.agenda_source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-6 flex items-center justify-center gap-2 rounded-sm border border-slate-700 px-3 py-2 text-xs font-medium uppercase tracking-wider text-slate-300 transition-colors hover:bg-slate-900"
          >
            <FileText className="h-3.5 w-3.5" />
            {pdfLabel}
          </a>
        </div>
      </aside>
    </>
  );
}
