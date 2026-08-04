import { getLeads, getScraperSources } from "@/lib/api";
import Dashboard from "@/components/Dashboard";

export default async function DashboardPage() {
  const [leads, sources] = await Promise.all([getLeads(), getScraperSources()]);

  return <Dashboard initialLeads={leads} initialSources={sources} />;
}
