"""POST /api/v1/scraper/run — scrapes configured municipal sources for new
agenda PDFs and feeds each one into the existing background ingestion
pipeline; GET /api/v1/scraper/sources lists the configured targets."""

import logging
from datetime import date, datetime
from uuid import uuid4

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

import local_store
from models.schemas import DocumentType
from routers.ingest import _run_ingest_job
from services.scraper import (
    SOURCES,
    MunicipalSource,
    download_document,
    find_new_documents,
)
from services.scraper_utils import get_default_headers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scraper", tags=["scraper"])

DOWNLOAD_TIMEOUT_SECONDS = 60.0

# The actual defense against pulling old/historical agendas is
# services/scraper.py's DEFAULT_LOOKBACK_DAYS (30 days) — every strategy's
# _resolve_date_window applies that bound whether or not this is a source's
# "first run", so find_new_documents already never returns anything older
# than that regardless of this cap's value. This is a separate, secondary
# ceiling: a hard cap on total *downloaded* PDFs for the whole run (summed
# across every source, not per-source) purely as an API-cost circuit
# breaker, in case an unusually large number of genuinely-recent documents
# come back at once across every configured jurisdiction. Raised from 3 to
# 50 so a full 21-source sweep isn't artificially truncated mid-run when
# verifying coverage — anything past the cap (still only ever within the
# lookback window) stays un-scraped (not marked as seen) and gets picked
# up on a later run.
MAX_TOTAL_DOCUMENTS_PER_SWEEP = 50


class ScraperJobSummary(BaseModel):
    job_id: str
    url: str
    title: str
    city: str


class ScraperRunResponse(BaseModel):
    documents_found: int
    jobs_started: int
    jobs: list[ScraperJobSummary]


class ScraperRunRequest(BaseModel):
    # Matches MunicipalSource.name (e.g. "Menlo Park City Council"). None
    # (or an omitted body) means "run every configured source" — the
    # pre-targeting default. An explicit empty list means the caller/UI
    # asked for a scan without picking any market, which is always an error
    # rather than silently running everything.
    jurisdictions: list[str] | None = None
    # Lookback-period filter from the Command Bar. None means no date
    # restriction (the "All Time" option) — enforced at the HTML-scraping
    # level in services/scraper.py, before any matching PDF is downloaded.
    since_date: date | None = None


@router.get("/sources", response_model=list[MunicipalSource])
async def list_sources() -> list[MunicipalSource]:
    return SOURCES


@router.post("/run", response_model=ScraperRunResponse)
async def run_scraper(
    *,
    payload: ScraperRunRequest | None = None,
    background_tasks: BackgroundTasks,
) -> ScraperRunResponse:
    """Scrapes the requested (or, absent a payload, every configured) source
    for new agenda/packet/staff-report PDFs dated on/after payload.since_date
    (skipping any URL already handled in a prior run) and queues them
    through the same background ingestion pipeline POST /api/v1/ingest uses
    — one job per document, each trackable via the existing
    GET /api/v1/ingest/status/{job_id} SSE endpoint. Stops downloading as
    soon as MAX_TOTAL_DOCUMENTS_PER_SWEEP documents have been downloaded,
    counted across every targeted source/jurisdiction combined, not per
    source."""
    if payload is not None and payload.jurisdictions is not None:
        if len(payload.jurisdictions) == 0:
            raise HTTPException(
                status_code=400,
                detail="Select at least one target market to run a scan.",
            )
        requested = set(payload.jurisdictions)
        sources_to_scan = [source for source in SOURCES if source.name in requested]
        if not sources_to_scan:
            raise HTTPException(
                status_code=400,
                detail="None of the selected markets match a configured source.",
            )
    else:
        sources_to_scan = SOURCES

    start_date = (
        datetime.combine(payload.since_date, datetime.min.time())
        if payload is not None and payload.since_date
        else None
    )
    documents = await find_new_documents(sources_to_scan, start_date=start_date)

    jobs: list[ScraperJobSummary] = []
    downloaded_count = 0
    async with AsyncSession(
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        headers=get_default_headers(),
        impersonate="chrome",
        allow_redirects=True,
    ) as client:
        for document in documents:
            if downloaded_count >= MAX_TOTAL_DOCUMENTS_PER_SWEEP:
                break

            try:
                file_path = await download_document(document.pdf_url, client)
            except RequestException:
                logger.exception("Failed to download scraped document %s", document.pdf_url)
                continue

            downloaded_count += 1

            title = document.title or document.pdf_url

            # Marked immediately after a successful download, independent
            # of whether the ingestion job below later succeeds, fails, or
            # is cancelled — dedup is "have we fetched this URL", not
            # "did we successfully process it", so a scraper run never
            # re-hits the same document repeatedly.
            local_store.mark_scraped_url(document.pdf_url)

            job_id = str(uuid4())
            local_store.create_job(job_id, message=f"Queued from scraper: {title}")
            background_tasks.add_task(
                _run_ingest_job,
                job_id,
                file_path,
                # The real, distinct PDF URL — not `title`, which is often
                # a generic label ("Agenda packet(PDF, 153KB)") shared by
                # many unrelated documents across different meetings.
                # documents.file_url is unique, so passing a non-distinct
                # value here would make an upsert silently merge separate
                # documents together instead of just deduplicating a
                # genuine re-scrape of the same one.
                document.pdf_url,
                document.jurisdiction,
                DocumentType.CITY_COUNCIL_AGENDA,
            )
            jobs.append(
                ScraperJobSummary(
                    job_id=job_id,
                    url=document.pdf_url,
                    title=title,
                    city=document.jurisdiction,
                )
            )

    return ScraperRunResponse(
        documents_found=len(documents), jobs_started=len(jobs), jobs=jobs
    )
