"""Pydantic models shared across the ingest, parcels, and leads routers."""

from datetime import date, datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SignalStrength(str, Enum):
    HIGH = "HIGH"
    MED = "MED"
    LOW = "LOW"


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
    apn: Optional[str] = Field(default=None, description="APN in XXX-XXX-XXX format")
    matched_keyword: str
    signal_strength: SignalStrength
    extracted_text_snippet: str
    page_number: int
    current_zoning: Optional[str] = None
    proposed_zoning: Optional[str] = None
    address: Optional[str] = None


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
    parcel_id: UUID
    document_id: UUID
    signal_strength: SignalStrength
    summary: str
    extracted_text_snippet: Optional[str] = None


class RezoningLead(RezoningLeadBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class RezoningLeadDetail(RezoningLead):
    """Rezoning lead joined with its parcel for dashboard table display."""

    parcel: Parcel
    agenda_source_url: str
