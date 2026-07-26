"""GET /api/v1/parcels — parcel lookups by APN and city."""

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from db import get_supabase
from models.schemas import Parcel

router = APIRouter(prefix="/api/v1/parcels", tags=["parcels"])


@router.get("", response_model=list[Parcel])
async def list_parcels(
    city: str | None = Query(default=None),
    supabase: Client = Depends(get_supabase),
) -> list[Parcel]:
    query = supabase.table("parcels").select("*")
    if city:
        query = query.eq("city", city)
    return query.execute().data


@router.get("/{apn}", response_model=Parcel)
async def get_parcel(apn: str, supabase: Client = Depends(get_supabase)) -> Parcel:
    rows = supabase.table("parcels").select("*").eq("apn", apn).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail=f"No parcel found for APN {apn}")
    return rows[0]
