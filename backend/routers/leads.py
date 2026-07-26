"""GET /api/v1/leads — rezoning leads joined with parcel + source document."""

from fastapi import APIRouter, Depends, Query
from supabase import Client

from db import get_supabase
from models.schemas import RezoningLeadDetail, SignalStrength

router = APIRouter(prefix="/api/v1/leads", tags=["leads"])

_SELECT = "*, parcel:parcels(*), document:documents(file_url)"


@router.get("", response_model=list[RezoningLeadDetail])
async def list_leads(
    signal_strength: SignalStrength | None = Query(default=None),
    supabase: Client = Depends(get_supabase),
) -> list[RezoningLeadDetail]:
    query = supabase.table("rezoning_leads").select(_SELECT)
    if signal_strength:
        query = query.eq("signal_strength", signal_strength.value)

    rows = query.order("created_at", desc=True).execute().data

    return [
        RezoningLeadDetail(
            **{k: v for k, v in row.items() if k != "document"},
            agenda_source_url=row["document"]["file_url"],
        )
        for row in rows
    ]
