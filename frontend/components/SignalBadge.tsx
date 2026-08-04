import type { SignalStrength } from "@/types";

const STYLES: Record<SignalStrength, string> = {
  HIGH: "bg-red-500/15 text-red-400 ring-1 ring-inset ring-red-500/30",
  MED: "bg-amber-500/15 text-amber-400 ring-1 ring-inset ring-amber-500/30",
  LOW: "bg-slate-500/15 text-slate-400 ring-1 ring-inset ring-slate-500/30",
  EXCLUDED: "bg-slate-800 text-slate-500 ring-1 ring-inset ring-slate-700 line-through",
};

export default function SignalBadge({ strength }: { strength: SignalStrength }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium ${STYLES[strength]}`}
    >
      {strength}
    </span>
  );
}
