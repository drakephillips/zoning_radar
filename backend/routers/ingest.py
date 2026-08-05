"""POST /api/v1/ingest — accepts a municipal PDF and runs extraction as a
background job; GET /api/v1/ingest/status/{job_id} streams its progress."""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

import local_store
from mem_diagnostics import peak_rss_mb
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
from services.parcel_resolver import resolve_apns_from_address
from services.pdf_parser import IngestCancelled, extract_signals_from_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])

# How often the SSE endpoint polls job_store for an update.
STATUS_POLL_INTERVAL_SECONDS = 1

UPLOAD_CHUNK_SIZE_BYTES = 1024 * 1024


async def _save_upload_to_tempfile(file: UploadFile) -> str:
    """Streams a manually-uploaded PDF to our own temp file in fixed-size
    chunks, mirroring services/scraper.py's download_document — never
    buffers the whole upload in memory at once. Written to a file we own
    (rather than trusting UploadFile's own internal temp file) because
    that one isn't guaranteed to survive past this request once
    BackgroundTasks actually runs the job later."""
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await file.read(UPLOAD_CHUNK_SIZE_BYTES):
                f.write(chunk)
    except Exception:
        os.unlink(path)
        raise
    return path


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
    exist, so lead_parcels.parcel_id's foreign key can be satisfied for
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
            resolved_parcel_ids: list[str] = []
            summary = (
                f"Policy amendment affecting {', '.join(signal.affected_districts)}"
                if signal.affected_districts
                else f"Policy amendment: {signal.matched_keyword}"
            )
        else:
            # A single printed APN becomes a one-element list; otherwise
            # resolve_apns_from_address may return several (an assemblage
            # of adjacent lots, or several APNs sharing one situs address
            # — both confirmed live) — a lead is never forced onto just
            # one parcel when the source material genuinely names more.
            resolved_apns = [signal.apn] if signal.apn else []
            try:
                if not resolved_apns and signal.address:
                    # No APN was printed in the source document — try to
                    # resolve one (or more) just-in-time from the address
                    # instead of dropping an otherwise-real lead (confirmed
                    # live this session: a real scrape produced 27 genuine
                    # site-specific leads with real addresses and no APN,
                    # none of which could persist before this).
                    resolved_apns = resolve_apns_from_address(signal.address, city_name)
                    if resolved_apns:
                        logger.info(
                            "Resolved APN(s) %s for %r via JIT lookup — no APN was printed "
                            "in the source document.",
                            resolved_apns,
                            signal.address,
                        )

                if not resolved_apns:
                    continue

                # Guarantees a parcels row exists for every resolved APN
                # before the lead_parcels inserts below need to satisfy
                # their foreign key — necessary whether an APN came from
                # Gemini directly or from the JIT lookup just now, since
                # either way the parcels table may have no matching row yet.
                for apn in resolved_apns:
                    _ensure_parcel_exists(
                        apn,
                        address=signal.address
                        or f"Unmatched — parsed from page {signal.page_number}",
                        city=signal.city or city_name,
                        current_zoning=signal.current_zoning,
                        proposed_zoning=signal.proposed_zoning,
                        max_units=signal.unit_count,
                    )
            except Exception:
                # Covers both resolve_apns_from_address (a network call to
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
                supabase.table("parcels").select("id").in_("apn", resolved_apns).execute().data
            )
            if not parcel_rows:
                continue
            resolved_parcel_ids = [row["id"] for row in parcel_rows]
            summary = (
                f"Matched '{signal.matched_keyword}' near APN {resolved_apns[0]}"
                if len(resolved_apns) == 1
                else f"Matched '{signal.matched_keyword}' spanning {len(resolved_apns)} "
                f"parcels ({', '.join(resolved_apns)})"
            )

        # Inserted exactly once regardless of how many parcels this lead
        # spans — see lead_parcels below, which is what links it to each
        # of resolved_parcel_ids. This is the actual fix for the old bug:
        # a multi-parcel lead used to get forced onto a single parcel_id
        # column instead of one lead row fanning out to several links.
        lead_row = (
            supabase.table("rezoning_leads")
            .insert(
                {
                    "document_id": document_id,
                    "lead_type": signal.lead_type.value,
                    "signal_strength": signal.signal_strength.value,
                    "summary": summary,
                    "extracted_text_snippet": signal.extracted_text_snippet,
                    "entitlement_type": signal.entitlement_type,
                    "affected_districts": signal.affected_districts,
                    # rezoning_leads.meeting_date is a strict `date` column
                    # — fall back to the parent document's meeting_date
                    # (set above) when this specific signal didn't have
                    # its own, so a lead is never left with a null date it
                    # could otherwise have inherited from the document it
                    # came from.
                    "meeting_date": signal.meeting_date or document_meeting_date,
                    "page_number": signal.page_number,
                }
            )
            .execute()
            .data[0]
        )

        if resolved_parcel_ids:
            supabase.table("lead_parcels").insert(
                [
                    {"lead_id": lead_row["id"], "parcel_id": parcel_id}
                    for parcel_id in resolved_parcel_ids
                ]
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
    pdf_path: str,
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
    documents could share (see routers/scraper.py's call site).

    pdf_path is a temp file on disk (see services/scraper.py's
    download_document and _save_upload_to_tempfile below) — this function
    owns its lifecycle and always deletes it before returning, success or
    failure, so temp files never accumulate across runs."""

    def progress_callback(message: str) -> None:
        local_store.update_job_progress(job_id, message)

    def should_cancel() -> bool:
        return local_store.is_cancelled(job_id)

    logger.info("[mem] starting ingest job %s for %r: %.1f MB", job_id, file_url, peak_rss_mb())
    try:
        signals, doc_type, excluded_count = extract_signals_from_pdf(
            pdf_path,
            city_name=city_name,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        logger.info(
            "[mem] extract_signals_from_pdf returned (%d signals): %.1f MB",
            len(signals),
            peak_rss_mb(),
        )

        if not is_dev_mode():
            try:
                result = _persist_to_supabase(
                    signals, doc_type, excluded_count, city_name, document_type, file_url
                )
                result = _log_and_annotate_zero_leads(result, city_name)
                local_store.complete_job(job_id, result.model_dump(mode="json"))
                logger.info("[mem] ingest job %s complete: %.1f MB", job_id, peak_rss_mb())
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
    finally:
        # Always runs — success, cancellation, or failure — so a temp PDF
        # never outlives the job that downloaded/uploaded it.
        try:
            os.unlink(pdf_path)
        except OSError:
            logger.warning("Failed to delete temp PDF %s", pdf_path, exc_info=True)


@router.post("", response_model=IngestJobCreated, status_code=202)
async def ingest_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    city_name: str,
    document_type: DocumentType,
) -> IngestJobCreated:
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only application/pdf uploads are supported")

    # Streamed to our own temp file now, synchronously, before returning —
    # UploadFile's underlying temp file isn't guaranteed to survive past
    # this request once BackgroundTasks runs, so the background job gets a
    # path to a file we own instead of the UploadFile itself. _run_ingest_job
    # deletes it when done, same as a scraper-downloaded PDF.
    file_path = await _save_upload_to_tempfile(file)

    job_id = str(uuid4())
    local_store.create_job(job_id, message="Queued")

    background_tasks.add_task(
        _run_ingest_job, job_id, file_path, file.filename or "upload.pdf", city_name, document_type
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
