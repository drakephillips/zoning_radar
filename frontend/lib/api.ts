import type { MunicipalSource, RezoningLeadDetail } from "@/types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * Fetches rezoning leads from the FastAPI backend. Returns an empty list
 * (rather than throwing) when the backend is unreachable, so the dashboard
 * still renders its empty state instead of crashing during local setup.
 */
export async function getLeads(): Promise<RezoningLeadDetail[]> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/v1/leads`, {
      cache: "no-store",
    });
    if (!res.ok) return [];
    return (await res.json()) as RezoningLeadDetail[];
  } catch {
    return [];
  }
}

/**
 * Lists the municipal sources the scraper is configured to poll. Returns an
 * empty list (rather than throwing) when the backend is unreachable, so the
 * control center still renders instead of crashing during local setup.
 */
export async function getScraperSources(): Promise<MunicipalSource[]> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/v1/scraper/sources`, {
      cache: "no-store",
    });
    if (!res.ok) return [];
    return (await res.json()) as MunicipalSource[];
  } catch {
    return [];
  }
}
