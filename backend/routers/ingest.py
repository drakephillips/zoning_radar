"""POST /api/v1/ingest — accepts a municipal PDF and runs extraction as a
background job; GET /api/v1/ingest/status/{job_id} streams its progress."""

import asyncio
import io
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

import local_store
from db import SUPABASE_UNAVAILABLE_ERRORS, get_supabase, is_dev_mode
from models.schemas import (
    DocumentClassification,
    DocumentProcessedStatus,
    DocumentType,
    ExtractedParcelSignal,
    IngestJobCreated,
    IngestResponse,
    LeadType,
    Parcel,
)
from services.parcel_resolver import resolve_apn_from_address
from services.pdf_parser import IngestCancelled, extract_signals_from_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])

# How often the SSE endpoint polls job_store for an update.
STATUS_POLL_INTERVAL_SECONDS = 1


def _build_fallback_parcel(signal: ExtractedParcelSignal, city_name: str) -> Parcel | None:
    """Synthesizes a Parcel record from an extracted signal for the
    in-memory store, mirroring what the frontend builds client-side for
    dev-mode display (see IngestDropzone.tsx's buildDevModeLeads)."""
    if signal.apn is None:
        return None
    now = datetime.now(timezone.utc)
    return Parcel(
        id=uuid4(),
        apn=signal.apn,
        address=signal.address or f"Unmatched — parsed from page {signal.page_number}",
        city=signal.city or city_name,
        current_zoning=signal.current_zoning,
        proposed_zoning=signal.proposed_zoning,
        max_units=signal.unit_count,
        created_at=now,
        updated_at=now,
    )


def _ensure_parcel_exists(
    apn: str,
    address: str,
    city: str,
    current_zoning: str | None = None,
    proposed_zoning: str | None = None,
    max_units: int | None = None,
) -> None:
    """Upserts a minimal parcels row for `apn` if one doesn't already
    exist, so rezoning_leads.parcel_id's foreign key can be satisfied for
    a lead whose APN was just resolved just-in-time (see
    services/parcel_resolver.py) rather than already present in the
    table. ignore_duplicates=True gives ON CONFLICT DO NOTHING — this
    function's job is only to guarantee a row exists, never to overwrite
    a richer parcel record (e.g. from real assessor data) with whatever
    minimal fields a single lead happened to have."""
    get_supabase().table("parcels").upsert(
        {
            "apn": apn,
            "address": address,
            "city": city,
            "current_zoning": current_zoning,
            "proposed_zoning": proposed_zoning,
            "max_units": max_units,
        },
        on_conflict="apn",
        ignore_duplicates=True,
    ).execute()


def _log_and_annotate_zero_leads(result: IngestResponse, city_name: str) -> IngestResponse:
    """If this document produced zero leads, logs a clear scan-completion
    message naming the jurisdiction and annotates the response with a
    user-facing `message` so the frontend can show something more
    informative than a silently empty leads list. No-op otherwise."""
    if result.leads_found != 0:
        return result

    logger.info("Scan complete for %s. 0 valuable development leads found.", city_name)
    return result.model_copy(
        update={
            "message": "Scan complete. No actionable development leads found in recent agendas."
        }
    )


def _persist_to_supabase(
    signals: list[ExtractedParcelSignal],
    doc_type: DocumentClassification,
    excluded_count: int,
    city_name: str,
    document_type: DocumentType,
    file_url: str,
) -> IngestResponse:
    supabase = get_supabase()

    # The whole agenda packet covers one meeting, so every signal extracted
    # from it should share the same date — used as the document's own
    # meeting_date, and as the fallback below for any individual lead whose
    # own meeting_date came back null (or malformed and got nulled out by
    # pdf_parser.py's validator — see _GeminiLeadItem.meeting_date). Every
    # signal.meeting_date reaching this function is guaranteed to be either
    # a real "YYYY-MM-DD" string or None, never something that would fail
    # to cast into the database's `date` columns.
    document_meeting_date = next(
        (signal.meeting_date for signal in signals if signal.meeting_date is not None),
        None,
    )

    # documents.file_url is unique — a daily delta scraper run can
    # legitimately re-encounter the same document (e.g. after a restart
    # clears the in-memory scraped-URL dedup set), so this upserts rather
    # than a plain insert, which would otherwise raise a Postgres unique-
    # violation and crash the whole ingest job.
    document_row = (
        supabase.table("documents")
        .upsert(
            {
                "city_name": city_name,
                "document_type": document_type.value,
                "file_url": file_url,
                "meeting_date": document_meeting_date,
                "processed_status": DocumentProcessedStatus.PROCESSING.value,
            },
            on_conflict="file_url",
        )
        .execute()
        .data[0]
    )
    document_id = document_row["id"]

    # Purges every lead this document previously produced before inserting
    # this run's results, so re-ingesting an already-processed document
    # (e.g. a daily delta scraper re-encountering it after a restart clears
    # the in-memory scraped-URL dedup set) replaces stale leads instead of
    # accumulating duplicates alongside them. Unconditional — runs even
    # when this run finds zero signals, so a document that no longer
    # yields any real lead correctly ends up with none, rather than keeping
    # whatever an earlier run had inserted.
    supabase.table("rezoning_leads").delete().eq("document_id", document_id).execute()

    leads_created = 0
    for signal in signals:
        if signal.lead_type == LeadType.POLICY_AMENDMENT:
            # A macro policy change has no single subject parcel — never
            # gated on an APN/parcel match, unlike SITE_SPECIFIC below.
            parcel_id = None
            summary = (
                f"Policy amendment affecting {', '.join(signal.affected_districts)}"
                if signal.affected_districts
                else f"Policy amendment: {signal.matched_keyword}"
            )
        else:
            resolved_apn = signal.apn
            try:
                if resolved_apn is None and signal.address:
                    # No APN was printed in the source document — try to
                    # resolve one just-in-time from the address instead
                    # of dropping an otherwise-real lead (confirmed live
                    # this session: a real scrape produced 27 genuine
                    # site-specific leads with real addresses and no APN,
                    # none of which could persist before this).
                    resolved_apn = resolve_apn_from_address(signal.address, city_name)
                    if resolved_apn:
                        logger.info(
                            "Resolved APN %s for %r via JIT lookup — no APN was printed "
                            "in the source document.",
                            resolved_apn,
                            signal.address,
                        )

                if resolved_apn is None:
                    continue

                # Guarantees a parcels row exists for resolved_apn before
                # the lead insert below needs to satisfy its foreign key
                # — necessary whether resolved_apn came from Gemini
                # directly or from the JIT lookup just now, since either
                # way the parcels table may have no matching row yet.
                _ensure_parcel_exists(
                    resolved_apn,
                    address=signal.address or f"Unmatched — parsed from page {signal.page_number}",
                    city=signal.city or city_name,
                    current_zoning=signal.current_zoning,
                    proposed_zoning=signal.proposed_zoning,
                    max_units=signal.unit_count,
                )
            except Exception:
                # Covers both resolve_apn_from_address (a network call to
                # two external, third-party services) and
                # _ensure_parcel_exists (a Supabase write) — neither
                # should ever be able to take down the rest of this
                # document's ingest run, let alone the whole sweep.
                logger.warning(
                    "JIT APN resolution/parcel upsert failed for address %r — skipping "
                    "this lead rather than risk crashing the ingest run.",
                    signal.address,
                    exc_info=True,
                )
                continue

            parcel_rows = (
                supabase.table("parcels").select("id").eq("apn", resolved_apn).execute().data
            )
            if not parcel_rows:
                continue
            parcel_id = parcel_rows[0]["id"]
            summary = f"Matched '{signal.matched_keyword}' near APN {resolved_apn}"

        supabase.table("rezoning_leads").insert(
            {
                "parcel_id": parcel_id,
                "document_id": document_id,
                "lead_type": signal.lead_type.value,
                "signal_strength": signal.signal_strength.value,
                "summary": summary,
                "extracted_text_snippet": signal.extracted_text_snippet,
                "entitlement_type": signal.entitlement_type,
                "affected_districts": signal.affected_districts,
                # rezoning_leads.meeting_date is a strict `date` column —
                # fall back to the parent document's meeting_date (set
                # above) when this specific signal didn't have its own, so
                # a lead is never left with a null date it could otherwise
                # have inherited from the document it came from.
                "meeting_date": signal.meeting_date or document_meeting_date,
                "page_number": signal.page_number,
            }
        ).execute()
        leads_created += 1

    supabase.table("documents").update(
        {"processed_status": DocumentProcessedStatus.PROCESSED.value}
    ).eq("id", document_id).execute()

    return IngestResponse(
        document_id=document_id,
        processed_status=DocumentProcessedStatus.PROCESSED,
        leads_found=leads_created,
        doc_type=doc_type,
        excluded_count=excluded_count,
    )


def _run_ingest_job(
    job_id: str,
    file_bytes: bytes,
    file_url: str,
    city_name: str,
    document_type: DocumentType,
) -> None:
    """The actual extraction + persistence work, run via BackgroundTasks
    (off the request/response cycle) so a multi-minute OCR run never blocks
    the HTTP response. Reports progress into job_store as it goes; the SSE
    endpoint below just polls that store.

    file_url must be a stable, genuinely-unique reference to the source
    document — it becomes documents.file_url, which now has a UNIQUE
    constraint (see database/schema.sql). For a manual upload that's the
    uploaded filename; for a scraped document it must be the real,
    distinct pdf_url, never a generic display title that different
    documents could share (see routers/scraper.py's call site)."""

    def progress_callback(message: str) -> None:
        local_store.update_job_progress(job_id, message)

    def should_cancel() -> bool:
        return local_store.is_cancelled(job_id)

    try:
        signals, doc_type, excluded_count = extract_signals_from_pdf(
            io.BytesIO(file_bytes),
            city_name=city_name,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )

        if not is_dev_mode():
            try:
                result = _persist_to_supabase(
                    signals, doc_type, excluded_count, city_name, document_type, file_url
                )
                result = _log_and_annotate_zero_leads(result, city_name)
                local_store.complete_job(job_id, result.model_dump(mode="json"))
                return
            except SUPABASE_UNAVAILABLE_ERRORS as exc:
                logger.warning(
                    "Supabase unreachable (%s: %s) — falling back to an in-memory dev-mode "
                    "response instead of failing the upload",
                    type(exc).__name__,
                    exc,
                )

        # Either dev mode from the start, or Supabase was unreachable
        # above: skip persistence, keep a local snapshot so GET
        # /api/v1/parcels can still serve this data, and hand the parsed
        # signals straight back.
        fallback_parcels = [
            parcel
            for parcel in (_build_fallback_parcel(signal, city_name) for signal in signals)
            if parcel is not None
        ]
        local_store.upsert_parcels(fallback_parcels)

        result = IngestResponse(
            document_id=uuid4(),
            processed_status=DocumentProcessedStatus.PROCESSED,
            leads_found=len(signals),
            dev_mode=True,
            extracted_signals=signals,
            doc_type=doc_type,
            excluded_count=excluded_count,
        )
        result = _log_and_annotate_zero_leads(result, city_name)
        local_store.complete_job(job_id, result.model_dump(mode="json"))
    except IngestCancelled:
        # job_store already reflects "cancelled" — that's what set
        # should_cancel() to True in the first place. Nothing further to
        # record; just stop working.
        logger.info("Ingest job %s stopped: cancelled by user", job_id)
    except Exception as exc:
        logger.exception("Ingest job %s failed", job_id)
        local_store.fail_job(job_id, str(exc))


@router.post("", response_model=IngestJobCreated, status_code=202)
async def ingest_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    city_name: str,
    document_type: DocumentType,
) -> IngestJobCreated:
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only application/pdf uploads are supported")

    # Read the upload into memory now, synchronously, before returning —
    # UploadFile's underlying temp file isn't guaranteed to survive past
    # this request once BackgroundTasks runs, so the background job gets
    # plain bytes instead of the UploadFile itself.
    file_bytes = await file.read()

    job_id = str(uuid4())
    local_store.create_job(job_id, message="Queued")

    background_tasks.add_task(
        _run_ingest_job, job_id, file_bytes, file.filename or "upload.pdf", city_name, document_type
    )

    return IngestJobCreated(job_id=job_id)


@router.get("/status/{job_id}")
async def ingest_status(job_id: str) -> StreamingResponse:
    async def event_stream():
        while True:
            job = local_store.get_job(job_id)
            if job is None:
                yield f"data: {json.dumps({'status': 'failed', 'message': 'Unknown job', 'result': None, 'error': 'Unknown job'})}\n\n"
                return

            yield f"data: {json.dumps(job)}\n\n"

            if job["status"] in ("completed", "failed", "cancelled"):
                return

            await asyncio.sleep(STATUS_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/status/{job_id}/cancel")
async def cancel_ingest(job_id: str) -> dict[str, bool]:
    """Marks a running job cancelled (the "End" button in the frontend's
    progress widget). The background job itself polls should_cancel()
    between OCR pages/Gemini calls (see _run_ingest_job) and stops there —
    it can't be killed mid-call, so a job already deep into a single
    Gemini request will still finish that one call before noticing."""
    cancelled = local_store.cancel_job(job_id)
    if not cancelled and local_store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return {"cancelled": cancelled}
