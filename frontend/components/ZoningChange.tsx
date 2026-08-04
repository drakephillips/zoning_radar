import { ArrowRight } from "lucide-react";

export default function ZoningChange({
  current,
  proposed,
}: {
  current: string | null;
  proposed: string | null;
}) {
  return (
    <div className="flex items-center gap-1.5 text-sm">
      <span className="text-slate-400">{current ?? "Unknown"}</span>
      <ArrowRight className="h-3.5 w-3.5 text-slate-600" />
      <span className="font-medium text-slate-100">{proposed ?? "Unknown"}</span>
    </div>
  );
}
