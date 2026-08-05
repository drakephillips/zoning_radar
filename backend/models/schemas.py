"""Pydantic models shared across the ingest, parcels, and leads routers."""

from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SignalStrength(str, Enum):
    HIGH = "HIGH"
    MED = "MED"
    LOW = "LOW"
    # Nearby exclusion language ("denied", "historic resource", etc.) means
    # this parcel is disqualified rather than a live lead.
    EXCLUDED = "EXCLUDED"


class LeadType(str, Enum):
    # A specific parcel entitlement (rezone, permit, variance, ...) — joins
    # to one or more Parcel rows via the lead_parcels junction table (a
    # development can span an assemblage of adjacent lots).
    SITE_SPECIFIC = "SITE_SPECIFIC"
    # A citywide or district-wide zoning code/General Plan change with no
    # single subject parcel (e.g. a Title 27 text amendment establishing new
    # zoning districts) — apn/address are null; affected_districts
    # describes the change's scope instead.
    POLICY_AMENDMENT = "POLICY_AMENDMENT"


# Document-level classification: does this PDF concern one specific parcel,
# or a citywide policy/ordinance packet that happens to touch many parcels?
DocumentClassification = Literal["SINGLE_SITE_APPLICATION", "POLICY_ORDINANCE"]


class DocumentType(str, Enum):
    CITY_COUNCIL_AGENDA = "CITY_COUNCIL_AGENDA"
    CITY_COUNCIL_MINUTES = "CITY_COUNCIL_MINUTES"
    PLANNING_COMMISSION_AGENDA = "PLANNING_COMMISSION_AGENDA"
    PLANNING_COMMISSION_MINUTES = "PLANNING_COMMISSION_MINUTES"
    STAFF_REPORT = "STAFF_REPORT"
    GENERAL_PLAN_AMENDMENT = "GENERAL_PLAN_AMENDMENT"
    OTHER = "OTHER"


class DocumentProcessedStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Extracted signals (produced by services/pdf_parser.py)
# ---------------------------------------------------------------------------
class ExtractedParcelSignal(BaseModel):
    apn: Optional[str] = Field(
        default=None,
        description=(
            "The Assessor's Parcel Number (APN). Must strictly follow numeric formats like "
            "'XXX-XXX-XXX' or 'XXX-XXXX-XXX'. Do NOT substitute an address. If a valid numeric "
            "APN is not explicitly stated in the document, you must return null."
        ),
    )
    matched_keyword: str
    signal_strength: SignalStrength
    extracted_text_snippet: str
    page_number: int
    current_zoning: Optional[str] = None
    proposed_zoning: Optional[str] = None
    address: Optional[str] = None
    # Set only for signals recovered from an exhibit/attachment APN table,
    # which rarely carries its own city name — falls back to the uploader's
    # city dropdown selection instead.
    city: Optional[str] = None
    # Populated by the Gemini extraction pipeline when the source text names
    # a specific unit count or entitlement type; the regex/heuristic
    # fallback pipeline leaves these unset.
    unit_count: Optional[int] = None
    entitlement_type: Optional[str] = None
    meeting_date: Optional[str] = Field(
        default=None,
        description=(
            "The exact date of the city council or planning commission meeting found on the "
            "agenda document. Format as YYYY-MM-DD. If not found, return null."
        ),
    )
    # Defaults to SITE_SPECIFIC so every signal the regex/heuristic fallback
    # pipeline produces (which only ever finds parcel/address-anchored
    # matches, never a macro policy narrative) needs no change at its
    # construction sites. The Gemini pipeline sets this explicitly per lead.
    lead_type: LeadType = LeadType.SITE_SPECIFIC
    # Only ever populated for a POLICY_AMENDMENT lead — the district(s),
    # plan area(s), or citywide scope a macro zoning change affects (e.g.
    # ["Downtown Precise Plan Area"] or ["Citywide"]). Always null for a
    # SITE_SPECIFIC lead, which has an actual parcel instead.
    affected_districts: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    city_name: str
    document_type: DocumentType
    meeting_date: Optional[date] = None
    file_url: str


class IngestResponse(BaseModel):
    document_id: UUID
    processed_status: DocumentProcessedStatus
    leads_found: int
    dev_mode: bool = False
    extracted_signals: list[ExtractedParcelSignal] = Field(default_factory=list)
    # Not to be confused with `document_type` (the source-document category
    # the uploader picks). This is auto-classified from the parsed content.
    doc_type: DocumentClassification = "SINGLE_SITE_APPLICATION"
    # Count of EXCLUDED signals filtered out of extracted_signals for
    # POLICY_ORDINANCE documents (always 0 otherwise).
    excluded_count: int = 0
    # User-facing completion note — set only when leads_found is 0, so the
    # frontend can show something more informative than a bare empty list
    # (see routers/ingest.py's _log_and_annotate_zero_leads).
    message: Optional[str] = None


class IngestJobCreated(BaseModel):
    """Immediate response from POST /api/v1/ingest — the actual extraction
    runs in a background task. Poll/stream GET /api/v1/ingest/status/{job_id}
    for progress and the eventual IngestResponse."""

    job_id: str


# ---------------------------------------------------------------------------
# Parcels
# ---------------------------------------------------------------------------
class ParcelBase(BaseModel):
    apn: str
    address: str
    city: str
    county: str = "San Mateo"
    current_zoning: Optional[str] = None
    proposed_zoning: Optional[str] = None
    max_far: Optional[float] = None
    max_units: Optional[int] = None
    owner_name: Optional[str] = None
    owner_address: Optional[str] = None


class Parcel(ParcelBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Rezoning leads (the alerts surfaced to the frontend dashboard)
# ---------------------------------------------------------------------------
class RezoningLeadBase(BaseModel):
    document_id: UUID
    lead_type: LeadType = LeadType.SITE_SPECIFIC
    signal_strength: SignalStrength
    summary: str
    extracted_text_snippet: Optional[str] = None
    entitlement_type: Optional[str] = None
    # Only populated for a POLICY_AMENDMENT lead — see LeadType.
    affected_districts: Optional[list[str]] = None
    meeting_date: Optional[str] = None
    # The source PDF page this signal was found on. Null for any row
    # inserted before this column existed; always set for a fresh ingest.
    page_number: Optional[int] = None


class RezoningLead(RezoningLeadBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class RezoningLeadDetail(RezoningLead):
    """Rezoning lead joined with the parcel(s) it's linked to (via the
    lead_parcels junction table — see database/schema.sql) for dashboard
    table display. A SITE_SPECIFIC lead can be linked to more than one
    parcel (an assemblage of adjacent lots, or several APNs sharing one
    situs address — see services/parcel_resolver.py); `parcels` is empty
    for a POLICY_AMENDMENT lead (see LeadType) — there is no single parcel
    to join against, so the frontend must render `affected_districts`
    instead whenever `parcels` is empty."""

    parcels: list[Parcel] = Field(default_factory=list)
    agenda_source_url: str
    # The source document's own city_name — always present (NOT NULL),
    # regardless of lead_type. Use this for a lead's Jurisdiction display
    # rather than parcel.city, which is null for a POLICY_AMENDMENT lead.
    jurisdiction: str
