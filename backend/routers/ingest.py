"""POST /api/v1/ingest — accepts a municipal PDF, scans it, and stores leads."""

from uuid import uuid4

from fastapi import APIRouter, HTTPException, UploadFile

from db import get_supabase, is_dev_mode
from models.schemas import (
    DocumentProcessedStatus,
    DocumentType,
    IngestResponse,
)
from services.pdf_parser import extract_signals_from_pdf

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


@router.post("", response_model=IngestResponse)
async def ingest_document(
    file: UploadFile,
    city_name: str,
    document_type: DocumentType,
) -> IngestResponse:
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only application/pdf uploads are supported")

    signals = extract_signals_from_pdf(file.file)

    if is_dev_mode():
        # No Supabase configured — skip persistence and hand the parsed
        # signals straight back so the dashboard can render them locally.
        return IngestResponse(
            document_id=uuid4(),
            processed_status=DocumentProcessedStatus.PROCESSED,
            leads_found=len(signals),
            dev_mode=True,
            extracted_signals=signals,
        )

    supabase = get_supabase()

    document_row = (
        supabase.table("documents")
        .insert(
            {
                "city_name": city_name,
                "document_type": document_type.value,
                "file_url": file.filename,
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
    )
