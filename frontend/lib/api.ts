import type { MunicipalSource, RezoningLeadDetail } from "@/types";

const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL;

// Falling back to localhost is only ever correct on a developer's own
// machine. In a deployed environment (Vercel, etc.) there's no local
// backend to reach, so the fallback silently produces empty data with no
// visible error — logging loudly here is what turns that into a
// diagnosable server-log line instead of "the dashboard is just empty."
if (!configuredApiUrl && process.env.NODE_ENV === "production") {
  console.error(
    "[lib/api] NEXT_PUBLIC_API_URL is not set — falling back to http://localhost:8000, " +
      "which is unreachable from a deployed environment. Set NEXT_PUBLIC_API_URL in your " +
      "Vercel project's Environment Variables to your deployed backend's URL.",
  );
}

export const API_BASE_URL = configuredApiUrl ?? "http://localhost:8000";

/** Shared GET-JSON-with-fallback used by every read below — logs the
 * specific failure (bad status vs. unreachable host) so it shows up in
 * Vercel's server function logs, then degrades to `fallback` rather than
 * throwing, so the dashboard still renders instead of crashing. */
async function fetchJson<T>(path: string, fallback: T): Promise<T> {
  try {
    const res = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
    if (!res.ok) {
      console.error(`[lib/api] ${path} returned HTTP ${res.status} from ${API_BASE_URL}`);
      return fallback;
    }
    return (await res.json()) as T;
  } catch (err) {
    console.error(`[lib/api] Failed to reach ${API_BASE_URL}${path}:`, err);
    return fallback;
  }
}

/** Fetches rezoning leads from the FastAPI backend. */
export async function getLeads(): Promise<RezoningLeadDetail[]> {
  return fetchJson<RezoningLeadDetail[]>("/api/v1/leads", []);
}

/** Lists the municipal sources the scraper is configured to poll. */
export async function getScraperSources(): Promise<MunicipalSource[]> {
  return fetchJson<MunicipalSource[]>("/api/v1/scraper/sources", []);
}
