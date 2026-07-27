"""FastAPI entry point for the Municipal & Zoning Intelligence Engine."""

import logging

from dotenv import load_dotenv

load_dotenv()

# Without this, module loggers (e.g. services.pdf_parser) have no handler
# attached anywhere in the hierarchy, so Gemini extraction failures on large
# PDFs were being logged but never actually printed anywhere visible.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import ingest, leads, parcels, scraper

app = FastAPI(
    title="Zoning Radar API",
    description="Parses municipal agendas and surfaces off-market rezoning leads.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    # Next.js falls back to 3001+ when 3000 is already taken by another
    # local process, so pin this to any localhost port rather than a single
    # hardcoded one — avoids NetworkError on the frontend whenever that
    # happens during local dev.
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(parcels.router)
app.include_router(leads.router)
app.include_router(scraper.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
