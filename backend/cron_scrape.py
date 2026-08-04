"""Standalone entrypoint for a scheduled scrape (e.g. a Render Cron Job).

Runs the same scrape -> download -> extract -> persist pipeline as
POST /api/v1/scraper/run, but as a one-shot script instead of an HTTP call —
a Render Cron Job starts a fresh process on a schedule and exits when done,
there's no long-running web server for it to send a request to.

Run from the backend/ directory (same convention as `uvicorn main:app`):
    python cron_scrape.py
"""

import asyncio
import logging
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cron_scrape")

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

import local_store
from models.schemas import DocumentType
from routers.ingest import _run_ingest_job
from routers.scraper import MAX_TOTAL_DOCUMENTS_PER_SWEEP
from services.scraper import SOURCES, download_document, find_new_documents
from services.scraper_utils import get_default_headers

DOWNLOAD_TIMEOUT_SECONDS = 60.0


async def run() -> None:
    documents = await find_new_documents(SOURCES)
    logger.info("Found %d new document(s) across %d source(s)", len(documents), len(SOURCES))

    downloaded_count = 0
    async with AsyncSession(
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        headers=get_default_headers(),
        impersonate="chrome",
        allow_redirects=True,
    ) as client:
        for document in documents:
            if downloaded_count >= MAX_TOTAL_DOCUMENTS_PER_SWEEP:
                logger.info(
                    "Hit MAX_TOTAL_DOCUMENTS_PER_SWEEP (%d) — stopping; remainder picked up "
                    "next run",
                    MAX_TOTAL_DOCUMENTS_PER_SWEEP,
                )
                break

            try:
                file_bytes = await download_document(document.pdf_url, client)
            except RequestException:
                logger.exception("Failed to download %s", document.pdf_url)
                continue

            downloaded_count += 1
            local_store.mark_scraped_url(document.pdf_url)

            job_id = str(uuid4())
            title = document.title or document.pdf_url
            local_store.create_job(job_id, message=f"Cron scrape: {title}")
            logger.info("Ingesting %s (%s)", title, document.jurisdiction)

            # Called directly rather than via FastAPI's BackgroundTasks (no
            # request/response cycle here to keep unblocked) — a cron
            # invocation can just run this synchronously and exit when done.
            _run_ingest_job(
                job_id,
                file_bytes,
                document.pdf_url,
                document.jurisdiction,
                DocumentType.CITY_COUNCIL_AGENDA,
            )

            job = local_store.get_job(job_id)
            if job and job["status"] == "failed":
                logger.error("Ingest failed for %s: %s", title, job["error"])

    logger.info("Cron scrape complete — downloaded %d document(s)", downloaded_count)


if __name__ == "__main__":
    asyncio.run(run())
