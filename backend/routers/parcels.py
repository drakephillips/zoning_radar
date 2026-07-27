"""GET /api/v1/parcels — parcel lookups by APN and city."""

import logging

from fastapi import APIRouter, HTTPException, Query

import local_store
from db import SUPABASE_UNAVAILABLE_ERRORS, get_supabase, is_dev_mode
from models.schemas import Parcel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/parcels", tags=["parcels"])


@router.get("", response_model=list[Parcel])
async def list_parcels(city: str | None = Query(default=None)) -> list[Parcel]:
    if is_dev_mode():
        # Known placeholder config (e.g. SUPABASE_SERVICE_ROLE_KEY unset) —
        # get_supabase() would fail client construction itself here, so
        # skip straight to the in-memory fallback rather than attempting it.
        return local_store.list_parcels(city=city)

    try:
        query = get_supabase().table("parcels").select("*")
        if city:
            query = query.eq("city", city)
        return query.execute().data
    except SUPABASE_UNAVAILABLE_ERRORS as exc:
        logger.warning(
            "Supabase unreachable (%s: %s) — serving parcels from the in-memory fallback store",
            type(exc).__name__,
            exc,
        )
        return local_store.list_parcels(city=city)


@router.get("/{apn}", response_model=Parcel)
async def get_parcel(apn: str) -> Parcel:
    if is_dev_mode():
        parcel = local_store.get_parcel(apn)
        if parcel is None:
            raise HTTPException(status_code=404, detail=f"No parcel found for APN {apn}")
        return parcel

    try:
        rows = get_supabase().table("parcels").select("*").eq("apn", apn).execute().data
        if not rows:
            raise HTTPException(status_code=404, detail=f"No parcel found for APN {apn}")
        return rows[0]
    except SUPABASE_UNAVAILABLE_ERRORS as exc:
        logger.warning(
            "Supabase unreachable (%s: %s) — checking the in-memory fallback store for APN %s",
            type(exc).__name__,
            exc,
            apn,
        )
        parcel = local_store.get_parcel(apn)
        if parcel is None:
            raise HTTPException(status_code=404, detail=f"No parcel found for APN {apn}") from exc
        return parcel
