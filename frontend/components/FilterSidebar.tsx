"use client";

import type { LeadFilters, LeadType, SignalStrength, SortOrder } from "@/types";
import { DEFAULT_FILTERS, toggleValue } from "@/lib/filters";

interface FilterSidebarProps {
  availableCities: string[];
  filters: LeadFilters;
  onChange: (filters: LeadFilters) => void;
}

const LEAD_TYPE_OPTIONS: { value: LeadType; label: string }[] = [
  { value: "SITE_SPECIFIC", label: "Site-Specific" },
  { value: "POLICY_AMENDMENT", label: "Policy Amendment" },
];

const SCORE_OPTIONS: { value: SignalStrength; label: string; dot: string }[] = [
  { value: "HIGH", label: "High", dot: "bg-red-400" },
  { value: "MED", label: "Med", dot: "bg-amber-400" },
  { value: "LOW", label: "Low", dot: "bg-slate-400" },
];

const SORT_OPTIONS: { value: SortOrder; label: string }[] = [
  { value: "recent", label: "Most Recent" },
  { value: "yield", label: "Highest Yield" },
];

function FilterGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-slate-800 py-4 first:pt-0 last:border-b-0 last:pb-0">
      <p className="mb-2.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
        {label}
      </p>
      {children}
    </div>
  );
}

function Checkbox({
  checked,
  onToggle,
  children,
}: {
  checked: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 py-0.5 text-sm text-slate-300 hover:text-slate-100">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        className="h-3.5 w-3.5 shrink-0 rounded-sm border-slate-600 bg-slate-800 text-emerald-500 accent-emerald-500 focus:ring-1 focus:ring-emerald-500 focus:ring-offset-0"
      />
      {children}
    </label>
  );
}

export default function FilterSidebar({ availableCities, filters, onChange }: FilterSidebarProps) {
  const isDefault =
    filters.selectedCities.length === 0 &&
    filters.selectedLeadTypes.length === 0 &&
    filters.selectedScores.length === 0 &&
    !filters.dateRange.start &&
    !filters.dateRange.end &&
    filters.sortOrder === DEFAULT_FILTERS.sortOrder;

  return (
    <aside className="flex flex-col rounded-sm border border-slate-800 bg-slate-900/60 p-5">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">Filters</h2>
        {!isDefault && (
          <button
            type="button"
            onClick={() => onChange(DEFAULT_FILTERS)}
            className="text-[11px] font-medium text-slate-500 transition-colors hover:text-emerald-400"
          >
            Reset
          </button>
        )}
      </div>

      <FilterGroup label="Sort By">
        <select
          value={filters.sortOrder}
          onChange={(e) => onChange({ ...filters, sortOrder: e.target.value as SortOrder })}
          className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-sm text-slate-100 focus:border-emerald-500 focus:outline-none"
        >
          {SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </FilterGroup>

      <FilterGroup label="Lead Type">
        <div className="flex flex-col gap-0.5">
          {LEAD_TYPE_OPTIONS.map((opt) => (
            <Checkbox
              key={opt.value}
              checked={filters.selectedLeadTypes.includes(opt.value)}
              onToggle={() =>
                onChange({
                  ...filters,
                  selectedLeadTypes: toggleValue(filters.selectedLeadTypes, opt.value),
                })
              }
            >
              {opt.label}
            </Checkbox>
          ))}
        </div>
      </FilterGroup>

      <FilterGroup label="Signal Score">
        <div className="flex flex-col gap-0.5">
          {SCORE_OPTIONS.map((opt) => (
            <Checkbox
              key={opt.value}
              checked={filters.selectedScores.includes(opt.value)}
              onToggle={() =>
                onChange({
                  ...filters,
                  selectedScores: toggleValue(filters.selectedScores, opt.value),
                })
              }
            >
              <span className="flex items-center gap-1.5">
                <span className={`h-1.5 w-1.5 rounded-full ${opt.dot}`} />
                {opt.label}
              </span>
            </Checkbox>
          ))}
        </div>
      </FilterGroup>

      <FilterGroup label="Meeting Date">
        <div className="flex flex-col gap-2">
          <label className="flex items-center justify-between gap-2 text-xs text-slate-500">
            From
            <input
              type="date"
              value={filters.dateRange.start ?? ""}
              onChange={(e) =>
                onChange({
                  ...filters,
                  dateRange: { ...filters.dateRange, start: e.target.value || null },
                })
              }
              className="w-[9.5rem] rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 font-mono text-xs text-slate-100 [color-scheme:dark] focus:border-emerald-500 focus:outline-none"
            />
          </label>
          <label className="flex items-center justify-between gap-2 text-xs text-slate-500">
            To
            <input
              type="date"
              value={filters.dateRange.end ?? ""}
              onChange={(e) =>
                onChange({
                  ...filters,
                  dateRange: { ...filters.dateRange, end: e.target.value || null },
                })
              }
              className="w-[9.5rem] rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 font-mono text-xs text-slate-100 [color-scheme:dark] focus:border-emerald-500 focus:outline-none"
            />
          </label>
        </div>
      </FilterGroup>

      <FilterGroup label="Jurisdiction">
        {availableCities.length === 0 ? (
          <p className="text-xs text-slate-600">No leads yet.</p>
        ) : (
          <div className="flex flex-col gap-0.5">
            {availableCities.map((city) => (
              <Checkbox
                key={city}
                checked={filters.selectedCities.includes(city)}
                onToggle={() =>
                  onChange({
                    ...filters,
                    selectedCities: toggleValue(filters.selectedCities, city),
                  })
                }
              >
                {city}
              </Checkbox>
            ))}
          </div>
        )}
      </FilterGroup>
    </aside>
  );
}
