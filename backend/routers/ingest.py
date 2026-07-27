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
    Parcel,
)
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


def _persist_to_supabase(
    signals: list[ExtractedParcelSignal],
    doc_type: DocumentClassification,
    excluded_count: int,
    city_name: str,
    document_type: DocumentType,
    filename: str,
) -> IngestResponse:
    supabase = get_supabase()

    document_row = (
        supabase.table("documents")
        .insert(
            {
                "city_name": city_name,
                "document_type": document_type.value,
                "file_url": filename,
                "processed_status": DocumentProcessedStatus.PROCESSING.value,
            }
        )
        .execute()
        .data[0]
    )
    document_id = document_row["id"]

    leads_created = 0
    for signal in signals:
        if signal.apn is None:
            continue
        parcel_rows = (
            supabase.table("parcels").select("id").eq("apn", signal.apn).execute().data
        )
        if not parcel_rows:
            continue

        supabase.table("rezoning_leads").insert(
            {
                "parcel_id": parcel_rows[0]["id"],
                "document_id": document_id,
                "signal_strength": signal.signal_strength.value,
                "summary": f"Matched '{signal.matched_keyword}' near APN {signal.apn}",
                "extracted_text_snippet": signal.extracted_text_snippet,
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
    filename: str,
    city_name: str,
    document_type: DocumentType,
) -> None:
    """The actual extraction + persistence work, run via BackgroundTasks
    (off the request/response cycle) so a multi-minute OCR run never blocks
    the HTTP response. Reports progress into job_store as it goes; the SSE
    endpoint below just polls that store."""

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
                    signals, doc_type, excluded_count, city_name, document_type, filename
                )
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
