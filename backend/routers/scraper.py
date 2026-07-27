"""POST /api/v1/scraper/run — scrapes configured municipal sources for new
agenda PDFs and feeds each one into the existing background ingestion
pipeline; GET /api/v1/scraper/sources lists the configured targets."""

import logging
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

import local_store
from models.schemas import DocumentType
from routers.ingest import _run_ingest_job
from services.scraper import (
    BROWSER_HEADERS,
    SOURCES,
    MunicipalSource,
    download_document,
    find_new_documents,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scraper", tags=["scraper"])

DOWNLOAD_TIMEOUT_SECONDS = 60.0

# Some listing pages (e.g. a CivicPlus AgendaCenter) return years of
# historical packets rather than just recently-posted ones, so on a source's
# very first run "new documents" can mean "the site's entire archive" across
# every configured jurisdiction — without a cap, one click would download +
# Gemini-extract all of them at once, burning through API credits in a
# single sweep. This is a hard ceiling on total *downloaded* PDFs for the
# whole run (summed across every source, not per-source), so it holds even
# when a multi-city sweep would otherwise pull well past it. Anything past
# the cap stays un-scraped (not marked as seen) and gets picked up on a
# later run.
MAX_TOTAL_DOCUMENTS_PER_SWEEP = 3


class ScraperJobSummary(BaseModel):
    job_id: str
    url: str
    title: str
    city: str


class ScraperRunResponse(BaseModel):
    documents_found: int
    jobs_started: int
    jobs: list[ScraperJobSummary]


@router.get("/sources", response_model=list[MunicipalSource])
async def list_sources() -> list[MunicipalSource]:
    return SOURCES


@router.post("/run", response_model=ScraperRunResponse)
async def run_scraper(background_tasks: BackgroundTasks) -> ScraperRunResponse:
    """Scrapes every configured source for new agenda/packet/staff-report
    PDFs (skipping any URL already handled in a prior run) and queues them
    through the same background ingestion pipeline POST /api/v1/ingest uses
    — one job per document, each trackable via the existing
    GET /api/v1/ingest/status/{job_id} SSE endpoint. Stops downloading as
    soon as MAX_TOTAL_DOCUMENTS_PER_SWEEP documents have been downloaded,
    counted across every source/jurisdiction combined, not per source."""
    documents = await find_new_documents()

    jobs: list[ScraperJobSummary] = []
    downloaded_count = 0
    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT_SECONDS, headers=BROWSER_HEADERS, follow_redirects=True
    ) as client:
        for document in documents:
            if downloaded_count >= MAX_TOTAL_DOCUMENTS_PER_SWEEP:
                break

            try:
                file_bytes = await download_document(document.url, client)
            except httpx.HTTPError:
                logger.exception("Failed to download scraped document %s", document.url)
                continue

            downloaded_count += 1

            # Marked immediately after a successful download, independent
            # of whether the ingestion job below later succeeds, fails, or
            # is cancelled — dedup is "have we fetched this URL", not
            # "did we successfully process it", so a scraper run never
            # re-hits the same document repeatedly.
            local_store.mark_scraped_url(document.url)

            job_id = str(uuid4())
            local_store.create_job(job_id, message=f"Queued from scraper: {document.title}")
            background_tasks.add_task(
                _run_ingest_job,
                job_id,
                file_bytes,
                document.title or "agenda.pdf",
                document.city,
                DocumentType.CITY_COUNCIL_AGENDA,
            )
            jobs.append(
                ScraperJobSummary(
                    job_id=job_id, url=document.url, title=document.title, city=document.city
                )
            )

    return ScraperRunResponse(
        documents_found=len(documents), jobs_started=len(jobs), jobs=jobs
    )
