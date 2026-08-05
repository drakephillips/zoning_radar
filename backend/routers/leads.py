"""GET /api/v1/leads — rezoning leads joined with parcel + source document."""

import logging

from fastapi import APIRouter, Query

import local_store
from db import SUPABASE_UNAVAILABLE_ERRORS, get_supabase, is_dev_mode
from models.schemas import RezoningLeadDetail, SignalStrength

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/leads", tags=["leads"])

_SELECT = "*, lead_parcels(parcel:parcels(*)), document:documents(file_url, city_name)"


@router.get("", response_model=list[RezoningLeadDetail])
async def list_leads(
    signal_strength: SignalStrength | None = Query(default=None),
) -> list[RezoningLeadDetail]:
    if is_dev_mode():
        # Known placeholder config (e.g. SUPABASE_SERVICE_ROLE_KEY unset) —
        # get_supabase() would fail client construction itself here, so
        # skip straight to the in-memory fallback rather than attempting it.
        return local_store.list_leads(signal_strength=signal_strength)

    try:
        query = get_supabase().table("rezoning_leads").select(_SELECT)
        if signal_strength:
            query = query.eq("signal_strength", signal_strength.value)

        rows = query.order("created_at", desc=True).execute().data

        return [
            RezoningLeadDetail(
                **{k: v for k, v in row.items() if k not in ("document", "lead_parcels")},
                # PostgREST returns the many-to-many embed as a list of
                # junction rows, each wrapping its joined parcel — flatten
                # that into the plain list of Parcels RezoningLeadDetail
                # expects. Empty for a POLICY_AMENDMENT lead, which has no
                # lead_parcels rows at all (see LeadType).
                parcels=[
                    lp["parcel"] for lp in (row.get("lead_parcels") or []) if lp.get("parcel")
                ],
                agenda_source_url=row["document"]["file_url"],
                # documents.city_name is NOT NULL, so this is always a real
                # value for every lead regardless of lead_type — unlike a
                # parcel's own city, which no POLICY_AMENDMENT lead has any
                # of (no single parcel). The frontend's Jurisdiction column
                # reads this field directly rather than falling back to a
                # parcel's city, so both lead types map correctly.
                jurisdiction=row["document"]["city_name"],
            )
            for row in rows
        ]
    except SUPABASE_UNAVAILABLE_ERRORS as exc:
        logger.warning(
            "Supabase unreachable (%s: %s) — serving leads from the in-memory fallback store",
            type(exc).__name__,
            exc,
        )
        return local_store.list_leads(signal_strength=signal_strength)
