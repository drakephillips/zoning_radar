import { Building2, Radar, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";

interface MetricCardProps {
  label: string;
  value: ReactNode;
  icon: ReactNode;
}

function MetricCard({ label, value, icon }: MetricCardProps) {
  return (
    <div className="flex items-center gap-4 rounded-sm border border-slate-800 bg-slate-900/60 p-5">
      <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-sm bg-slate-800 text-emerald-400">
        {icon}
      </div>
      <div className="min-w-0">
        <p className="font-mono text-2xl font-semibold text-slate-100">{value}</p>
        <p className="text-xs uppercase tracking-wider text-slate-500">{label}</p>
      </div>
    </div>
  );
}

interface MetricsBarProps {
  regionalPipeline: number;
  highYieldSignals: number;
  coverageArea: number;
}

export default function MetricsBar({
  regionalPipeline,
  highYieldSignals,
  coverageArea,
}: MetricsBarProps) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
      <MetricCard
        label="Regional Pipeline"
        value={`+${regionalPipeline}`}
        icon={<TrendingUp className="h-5 w-5" />}
      />
      <MetricCard
        label="High-Yield Signals"
        value={highYieldSignals}
        icon={<Radar className="h-5 w-5" />}
      />
      <MetricCard
        label="Coverage Area"
        value={coverageArea}
        icon={<Building2 className="h-5 w-5" />}
      />
    </div>
  );
}
