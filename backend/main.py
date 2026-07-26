"""FastAPI entry point for the Municipal & Zoning Intelligence Engine."""

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import ingest, leads, parcels

app = FastAPI(
    title="Zoning Radar API",
    description="Parses municipal agendas and surfaces off-market rezoning leads.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(parcels.router)
app.include_router(leads.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
