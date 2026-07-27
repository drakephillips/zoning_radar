"""In-memory fallback store for parcels, used when Supabase is unreachable
or unconfigured so the ingest -> dashboard flow still works end-to-end
without a live database. See routers/ingest.py (writes) and
routers/parcels.py, routers/leads.py (reads). Process-local and
non-persistent by design — this exists purely to keep local/dev usage
working, not as a real store.

Also holds `job_store`, tracking background ingestion jobs so the SSE
status endpoint (GET /api/v1/ingest/status/{job_id}) can report live
progress on a PDF that may take several minutes to OCR/extract. Same
process-local caveat applies — a job's state disappears if the server
restarts mid-run.

Also holds the scraped-URL dedup set used by services/scraper.py so a
scraper run never re-downloads or re-queues a PDF it's already handled."""

from typing import Any, Literal

from models.schemas import Parcel, RezoningLeadDetail, SignalStrength

JobStatus = Literal["processing", "completed", "failed", "cancelled"]

# {job_id: {"status": ..., "message": ..., "result": <dict|None>, "error": <str|None>}}
job_store: dict[str, dict[str, Any]] = {}

_parcels_by_apn: dict[str, Parcel] = {}


def upsert_parcels(parcels: list[Parcel]) -> None:
    for parcel in parcels:
        _parcels_by_apn[parcel.apn] = parcel


def list_parcels(city: str | None = None) -> list[Parcel]:
    parcels = list(_parcels_by_apn.values())
    if city:
        parcels = [p for p in parcels if p.city == city]
    return parcels


def get_parcel(apn: str) -> Parcel | None:
    return _parcels_by_apn.get(apn)


def list_leads(signal_strength: SignalStrength | None = None) -> list[RezoningLeadDetail]:
    """Leads to serve when Supabase is unreachable/unconfigured. Always
    empty for now: the ingest fallback path only persists Parcel records
    (see upsert_parcels above), not full lead detail — signal strength,
    summary, source document — so there's nothing to report honestly here
    yet rather than fabricating it. `signal_strength` is accepted (unused)
    to keep this function's shape matching the real Supabase-backed query
    it stands in for, so a future richer fallback-ingest can plug real
    data in here without touching either call site."""
    return []


def create_job(job_id: str, message: str = "Queued") -> None:
    job_store[job_id] = {"status": "processing", "message": message, "result": None, "error": None}


def update_job_progress(job_id: str, message: str) -> None:
    job = job_store.get(job_id)
    if job is not None:
        job["message"] = message


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    job_store[job_id] = {
        "status": "completed",
        "message": "Done",
        "result": result,
        "error": None,
    }


def fail_job(job_id: str, error: str) -> None:
    job_store[job_id] = {
        "status": "failed",
        "message": error,
        "result": None,
        "error": error,
    }


def cancel_job(job_id: str) -> bool:
    """Marks a job cancelled (e.g. the user hit "End" in the progress
    widget). Only takes effect while still processing — a job that's
    already completed/failed keeps that outcome. Returns whether the job
    existed and was actually still processing."""
    job = job_store.get(job_id)
    if job is None or job["status"] != "processing":
        return False
    job["status"] = "cancelled"
    job["message"] = "Cancelled"
    return True


def is_cancelled(job_id: str) -> bool:
    job = job_store.get(job_id)
    return job is not None and job["status"] == "cancelled"


def get_job(job_id: str) -> dict[str, Any] | None:
    return job_store.get(job_id)


# Every agenda/packet PDF URL the scraper has already downloaded and
# queued for ingestion, regardless of whether that ingestion job later
# succeeded, failed, or was cancelled — dedup is by "have we fetched this
# URL before", not by ingestion outcome, so a scraper run never hammers
# the same source URL repeatedly.
_scraped_urls: set[str] = set()


def is_scraped_url(url: str) -> bool:
    return url in _scraped_urls


def mark_scraped_url(url: str) -> None:
    _scraped_urls.add(url)
