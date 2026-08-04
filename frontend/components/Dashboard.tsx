"use client";

import { useMemo, useState } from "react";
import { Download, Radar } from "lucide-react";
import type { LeadFilters, MunicipalSource, RezoningLeadDetail } from "@/types";
import { applyFilters, applySort, DEFAULT_FILTERS, uniqueJurisdictions } from "@/lib/filters";
import { exportLeadsToCSV } from "@/lib/export";
import MetricsBar from "./MetricsBar";
import LeadsTable from "./LeadsTable";
import FilterSidebar from "./FilterSidebar";

interface DashboardProps {
  initialLeads: RezoningLeadDetail[];
  initialSources: MunicipalSource[];
}

export default function Dashboard({ initialLeads, initialSources }: DashboardProps) {
  const leads = initialLeads;
  const [filters, setFilters] = useState<LeadFilters>(DEFAULT_FILTERS);

  const activeLeads = leads.filter((lead) => lead.signal_strength !== "EXCLUDED");
  const regionalPipeline = activeLeads.reduce(
    (total, lead) => total + (lead.parcel?.max_units ?? 0),
    0,
  );
  const highYieldSignals = leads.filter(
    (lead) => lead.signal_strength === "HIGH" || lead.signal_strength === "MED",
  ).length;

  const availableCities = useMemo(() => uniqueJurisdictions(leads), [leads]);
  const visibleLeads = useMemo(
    () => applySort(applyFilters(leads, filters), filters.sortOrder),
    [leads, filters],
  );
  const isFiltering =
    filters.selectedCities.length > 0 ||
    filters.selectedLeadTypes.length > 0 ||
    filters.selectedScores.length > 0 ||
    filters.dateRange.start !== null ||
    filters.dateRange.end !== null;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-8">
      <header className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
          <Radar className="h-6 w-6" />
        </div>
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Zoning Radar</h1>
          <p className="text-sm text-slate-500">
            CRE Land Intelligence Terminal — San Mateo &amp; Bay Area
          </p>
        </div>
      </header>

      <MetricsBar
        regionalPipeline={regionalPipeline}
        highYieldSignals={highYieldSignals}
        coverageArea={initialSources.length}
      />

      {/* Filters (left, ~1/4 width) + Live Lead Feed (right, the remainder).
          self-start overrides the grid's default align-items:stretch so the
          sidebar is only as tall as its own filter content, not stretched
          to match the (much taller) table — it's a static block that
          scrolls with the page, not pinned in place. Stacks full-width on
          small screens, where a fixed 20-25% column wouldn't be usable. */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-4 lg:gap-6">
        <div className="self-start">
          <FilterSidebar
            availableCities={availableCities}
            filters={filters}
            onChange={setFilters}
          />
        </div>

        <div className="flex min-w-0 flex-col gap-3 lg:col-span-3">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
                Live Lead Feed
              </h2>
              <span className="flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
              <span className="font-mono text-xs text-slate-600">
                {visibleLeads.length}/{leads.length}
              </span>
            </div>
            <button
              type="button"
              onClick={() => exportLeadsToCSV(visibleLeads)}
              className="flex items-center gap-1.5 rounded-sm border border-slate-700 px-3 py-1.5 text-xs font-medium uppercase tracking-wider text-slate-300 transition-colors hover:border-emerald-500/50 hover:text-emerald-400"
            >
              <Download className="h-3.5 w-3.5" />
              Export to CSV
            </button>
          </div>
          <LeadsTable
            leads={visibleLeads}
            emptyMessage={isFiltering ? "No leads match the current filters." : undefined}
          />
        </div>
      </section>
    </main>
  );
}
