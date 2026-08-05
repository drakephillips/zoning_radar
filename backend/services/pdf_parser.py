"""Scans municipal PDF agendas/minutes for rezoning signals, APNs, zoning
transitions, and nearby street addresses.

Extraction runs through Gemini (see _extract_via_gemini) when GEMINI_API_KEY
is configured. Without a key, or if a Gemini call fails, everything falls
back to the deterministic regex/heuristic pipeline below (_extract_via_regex
and friends) — which is also what keeps local development and tests fully
offline, with no API cost or network dependency.

Text extraction (_extract_pdf_text) falls back to OCR (pytesseract) for any
page PyMuPDF can't get text from directly — necessary for scanned,
image-only municipal PDFs, which are common for older agendas. PyMuPDF
(fitz) both reads the PDF and rasterizes the one page OCR needs, so the
only system binary beyond the pip packages is the `tesseract` OCR engine
itself — e.g. `brew install tesseract` on macOS, `apt-get install
tesseract-ocr` on Debian/Ubuntu."""

import gc
import logging
import os
import re
import time
from datetime import date
from typing import Callable, Literal

import fitz
import pytesseract
from pydantic import BaseModel, Field, field_validator

from mem_diagnostics import peak_rss_mb
from models.schemas import DocumentClassification, ExtractedParcelSignal, LeadType, SignalStrength

logger = logging.getLogger(__name__)

# Reports human-readable progress ("Running OCR: Page 3 of 40", "Stage 1:
# Checking intent...") during a long-running ingest, so a caller (e.g. the
# background job in routers/ingest.py) can surface it live via SSE. Always
# optional — every function below works exactly as before when omitted.
ProgressCallback = Callable[[str], None]


def _report_progress(callback: ProgressCallback | None, message: str) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        logger.exception("progress_callback raised while reporting: %s", message)


class IngestCancelled(Exception):
    """Raised internally when `should_cancel()` reports the job was
    cancelled mid-run (e.g. the user hit "End" in the progress widget), so
    a long OCR/Gemini pipeline can bail out between pages/chunks instead of
    grinding through the rest of a multi-minute run pointlessly."""


# Checked between pages/chunks during a long-running ingest so a
# user-initiated cancellation actually stops work, rather than just
# hiding the frontend widget while the job keeps running regardless.
# Optional, same as ProgressCallback — omitting it just means the job
# always runs to completion, as before.
CancelCheck = Callable[[], bool]


def _check_cancelled(should_cancel: CancelCheck | None) -> None:
    if should_cancel is not None and should_cancel():
        raise IngestCancelled()


# San Mateo County APNs are formatted as three groups of digits, e.g. 042-311-090
APN_PATTERN = re.compile(r"\b(\d{3}-\d{3}-\d{3})\b")

# Full-shape gate for a Gemini-reported APN (see _GeminiLeadItem's
# apn validator below): matches exactly the two shapes the field's own
# description documents as valid — "XXX-XXX-XXX" (standard 3-3-3) or
# "XXX-XXXX-XXX" (a 4-digit middle group seen in some jurisdictions/OCR'd
# tables) — both always ending in a 3-digit final group. A US phone number
# is shaped XXX-XXX-XXXX (3-3-4, four digits *last*), which this pattern
# never matches — that mismatch is what makes it a reliable phone-number
# rejector, not just an "only digits and hyphens" check.
GEMINI_APN_SHAPE_PATTERN = re.compile(r"^\d{3}-\d{3,4}-\d{3}$")

# Zoning code, e.g. "R-1", "R1", "RM-20", "PD-2"
_ZONING_CODE = r"[A-Z]{1,3}-?\d{1,3}"

# A Title-Case zoning descriptor word, e.g. "Low-Density", "Residential", "Mixed-Use"
_ZONING_WORD = r"[A-Z][a-zA-Z]*(?:-[A-Za-z]+)?"
_ZONING_PHRASE = rf"{_ZONING_WORD}(?:\s+{_ZONING_WORD}){{0,3}}"

# A descriptive phrase only counts as a zoning term when it's tagged with a
# parenthetical code, e.g. "Low-Density Residential (R-1)" or
# "High-Density Mixed-Use (R-3)" — the parenthetical is what tells us this
# is a real zoning designation and not some other capitalized phrase.
_ZONING_PHRASE_WITH_CODE = rf"{_ZONING_PHRASE}\s*\(\s*{_ZONING_CODE}\s*\)"

# A bare descriptive phrase (no parenthetical code) still needs a
# density/family qualifier to count, e.g. "Low-Density to High-Density".
_DENSITY_QUALIFIER = r"(?:Low|Medium|High|Single|Multi|Two|Mixed)"
_ZONING_PHRASE_BARE = (
    rf"{_DENSITY_QUALIFIER}-(?:Density|Family)(?:\s+(?:Residential|Commercial|Use|Mixed-Use))?"
)

_ZONING_TOKEN = rf"(?:{_ZONING_PHRASE_WITH_CODE}|{_ZONING_PHRASE_BARE}|{_ZONING_CODE})"

# Matches "R-1 to R-3", "R1 to R3", "Low-Density to High-Density", and
# "Low-Density Residential (R-1) to High-Density Mixed-Use (R-3)".
# Deliberately case-sensitive: zoning codes and descriptor phrases are
# Title-Case/uppercase in practice, and that capitalization is exactly what
# tells a real zoning term apart from surrounding lowercase prose (e.g. the
# "from"/"zoning" in "changing zoning from Low-Density...").
#
# No trailing \b: a token ending in ")" (from a parenthetical code) is
# followed by whitespace, and two non-word characters never form a boundary
# — a trailing \b there would silently force the regex to backtrack onto
# the bare-phrase alternative and drop the parenthetical code.
ZONING_TRANSITION_PATTERN = re.compile(rf"\b({_ZONING_TOKEN})\s+to\s+({_ZONING_TOKEN})")

_STREET_SUFFIXES = (
    r"Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|"
    r"Way|Circle|Cir|Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy"
)
# El Camino Real runs through most of San Mateo County and carries no
# generic suffix, so it needs its own alternative to the suffix list above.
_NAMED_STREETS = r"El\s+Camino\s+Real"
ADDRESS_PATTERN = re.compile(
    # Negative lookbehind keeps this from starting mid-APN (e.g. the "090"
    # in "042-311-090"), which is always preceded by a hyphen, not a space.
    rf"(?<!-)\b(\d{{1,6}}\s+(?:(?:[A-Za-z0-9.'-]+\s+){{0,4}}(?:{_STREET_SUFFIXES})\.?|{_NAMED_STREETS}))\b",
    re.IGNORECASE,
)

# Fallback when no street-suffix address is found nearby: agenda staff
# reports commonly label the parcel address directly, e.g. "Location: 500 El
# Camino Real" or "Address: 1234 Broadway".
LABELED_ADDRESS_PATTERN = re.compile(r"(?:Location|Address)\s*:\s*([^\n]+)", re.IGNORECASE)

# Keyword -> signal strength. Stronger zoning-change language ranks higher.
# "Planned Community Permit" and "Vesting Tentative Map" are also entitlement
# keywords used to gate address-anchored leads below (see ENTITLEMENT_KEYWORDS)
# — they need to be triggering keywords here too, otherwise an item titled
# only "Planned Community Permit for 333 Main St" would never even reach
# that gate, defeating the point of it.
KEYWORD_SIGNAL_STRENGTH: dict[str, SignalStrength] = {
    "general plan amendment": SignalStrength.HIGH,
    "rezone": SignalStrength.HIGH,
    "rezoning": SignalStrength.HIGH,
    "planned community permit": SignalStrength.HIGH,
    "vesting tentative map": SignalStrength.HIGH,
    "density bonus": SignalStrength.MED,
    "specific plan": SignalStrength.MED,
    "zoning text amendment": SignalStrength.MED,
    "conditional use permit": SignalStrength.LOW,
    "variance": SignalStrength.LOW,
}

# EXCLUDED always wins dedup comparisons so a disqualified parcel can't be
# hidden behind some other, more "exciting" mention of the same APN.
_SIGNAL_STRENGTH_RANK = {
    SignalStrength.EXCLUDED: 4,
    SignalStrength.HIGH: 3,
    SignalStrength.MED: 2,
    SignalStrength.LOW: 1,
}

SNIPPET_RADIUS_CHARS = 160

# How far either side of the APN to look for zoning/address detail before
# falling back to scanning the entire page.
SEARCH_WINDOW_CHARS = 1000

# ---------------------------------------------------------------------------
# Section filtering — agendas are mostly procedural boilerplate. Extraction
# is restricted to Public Hearing item text when that section exists, and
# always skips recognized boilerplate sections. Boilerplate/hearing headers
# are matched case-sensitively (real agenda headers are printed in ALL
# CAPS), so a lowercase, casual mention like "the council held a public
# hearing on..." doesn't get mistaken for an actual section break. Exhibit
# headers are the exception (see _EXHIBIT_HEADER_PATTERN below) — real
# packets print those in Title Case as often as ALL CAPS.
# ---------------------------------------------------------------------------
_SECTION_UNKNOWN = "unknown"
_SECTION_BOILERPLATE = "boilerplate"
_SECTION_HEARING = "hearing"
_SECTION_EXHIBIT = "exhibit"

_BOILERPLATE_HEADER_PATTERN = re.compile(
    r"\b(?:CALL TO ORDER|ROLL CALL|PLEDGE OF ALLEGIANCE|APPROVAL OF (?:THE )?MINUTES|"
    r"CONSENT CALENDAR|PUBLIC COMMENTS?|ADJOURNMENT|CLOSED SESSION|STUDY SESSION)\b"
)
_HEARING_HEADER_PATTERN = re.compile(r"\bPUBLIC HEARINGS?\b")
# Ordinance packets attach exhibits/attachments (often APN tables) after the
# main narrative, commonly *after* an ADJOURNMENT. Recognizing their own
# header explicitly stops them from silently inheriting a stale
# "boilerplate" carry-in state and being excluded from scanning entirely.
# Case-insensitive and not anchored to line starts: real packets print these
# as "Exhibit A" (Title Case) inline in running text as often as as a
# standalone all-caps header, and topic phrases like "Zoning Map Amendment"
# or "Sites Temporarily Excluded" mark an exhibit just as reliably as an
# explicit "Exhibit"/"Attachment" label does.
_EXHIBIT_HEADER_PATTERN = re.compile(
    r"\b(?:Exhibit|Attachment)\s+[A-Z0-9]+\b"
    r"|\bZoning Map Amendment\b"
    r"|\bSites Temporarily Excluded\b",
    re.IGNORECASE,
)


def _section_spans(text: str, carry_in: str) -> tuple[list[tuple[int, int, str]], str]:
    """Splits `text` into (start, end, kind) spans based on recognized
    agenda section headers, carrying the active section kind in from the
    previous page (`carry_in`) for items that continue onto a page without
    repeating the header. Returns the spans plus the kind active at the end
    of this page, to carry into the next one."""
    markers = [(m.start(), _SECTION_BOILERPLATE) for m in _BOILERPLATE_HEADER_PATTERN.finditer(text)]
    markers += [(m.start(), _SECTION_HEARING) for m in _HEARING_HEADER_PATTERN.finditer(text)]
    markers += [(m.start(), _SECTION_EXHIBIT) for m in _EXHIBIT_HEADER_PATTERN.finditer(text)]
    markers.sort(key=lambda marker: marker[0])

    if not markers:
        return [(0, len(text), carry_in)], carry_in

    spans: list[tuple[int, int, str]] = []
    prev_pos, prev_kind = 0, carry_in
    for pos, kind in markers:
        if pos > prev_pos:
            spans.append((prev_pos, pos, prev_kind))
        prev_pos, prev_kind = pos, kind
    spans.append((prev_pos, len(text), prev_kind))
    return spans, prev_kind


def _eligible_spans(spans: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """Public Hearing sections take exclusive priority for the generic
    keyword scan when present; otherwise everything except boilerplate is
    eligible. Exhibit/attachment sections are deliberately excluded from
    this scan either way — "nearest APN to a keyword" is the wrong model
    for a parcel table, and an exhibit's own header text (e.g. "Sites
    Proposed for Rezoning") would otherwise trigger a partial, misleading
    match on just the first APN in the table. Exhibits get their own,
    thorough table parser instead (see _extract_apns_from_exhibits) — this
    function still tracks them as a distinct kind purely so carry-in state
    doesn't get corrupted across pages (see _section_spans)."""
    if any(kind == _SECTION_HEARING for _, _, kind in spans):
        return [(start, end) for start, end, kind in spans if kind == _SECTION_HEARING]
    return [
        (start, end)
        for start, end, kind in spans
        if kind not in (_SECTION_BOILERPLATE, _SECTION_EXHIBIT)
    ]


def _position_in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


# Agenda items are commonly numbered ("Item 5:", "Item No. 5"). Individual
# hearing items often sit close together on a page, so every proximity
# search below (zoning, address, exclusion language) is clipped to the
# current item's span — otherwise a wide char window can bleed a neighboring
# item's zoning transition or "denied" language onto the wrong parcel.
_ITEM_MARKER_PATTERN = re.compile(r"\bItem\s+(?:No\.?\s*)?\d+\b", re.IGNORECASE)


def _item_span(text: str, anchor: int) -> tuple[int, int]:
    start = 0
    for match in _ITEM_MARKER_PATTERN.finditer(text):
        if match.start() <= anchor:
            start = match.start()
        else:
            return start, match.start()
    return start, len(text)


# ---------------------------------------------------------------------------
# Address-anchored leads — when an item has no explicit APN, a validated
# street address in the item's own title/SUBJECT line can stand in for one,
# but only under tight conditions to avoid false positives: the address
# must come from the item's header (never general body text, which is far
# more likely to contain an unrelated address), must not be a known
# non-project address (city hall, applicant/consultant letterheads), and
# the item must name an actual entitlement type — not just any keyword.
# ---------------------------------------------------------------------------
_SUBJECT_LINE_PATTERN = re.compile(r"\bSUBJECT\s*:\s*([^\n]+)", re.IGNORECASE)

# Known non-project addresses that should never anchor a lead on their own.
# "1017 Middlefield" is Redwood City's City Hall, which turns up constantly
# in agenda letterheads/footers and is never itself the subject parcel.
ADDRESS_BLACKLIST = ("1017 Middlefield",)

ENTITLEMENT_KEYWORDS = (
    "planned community permit",
    "density bonus",
    "vesting tentative map",
    "rezone",
)


def _item_header_block(text: str, item_bounds: tuple[int, int]) -> str:
    """The item's own title line (the text right after "Item N:") plus any
    "SUBJECT:" line within its span — the only text address-anchored
    detection is allowed to read from. Deliberately excludes everything
    else in the item's body."""
    start, end = item_bounds
    item_text = text[start:end]

    title = ""
    title_match = _ITEM_MARKER_PATTERN.match(item_text)
    if title_match:
        title = item_text[title_match.end() :].split("\n", 1)[0].strip()

    subject_match = _SUBJECT_LINE_PATTERN.search(item_text)
    subject = subject_match.group(1).strip() if subject_match else ""

    return "\n".join(part for part in (title, subject) if part)


def _is_blacklisted_address(address: str) -> bool:
    lowered = address.lower()
    return any(blacklisted.lower() in lowered for blacklisted in ADDRESS_BLACKLIST)


def _extract_address_anchored_lead(
    text: str, item_bounds: tuple[int, int]
) -> tuple[str | None, str | None]:
    """Returns (synthetic_apn, address) for a validated address-anchored
    lead, or (None, None) if the header has no address, the address is
    blacklisted, or no entitlement keyword is present."""
    header_block = _item_header_block(text, item_bounds)
    if not header_block:
        return None, None

    address_match = ADDRESS_PATTERN.search(header_block)
    if address_match is None:
        return None, None
    address = address_match.group(1).strip()

    if _is_blacklisted_address(address):
        return None, None

    lowered_header = header_block.lower()
    if not any(keyword in lowered_header for keyword in ENTITLEMENT_KEYWORDS):
        return None, None

    return f"ADDR: {address}", address


# ---------------------------------------------------------------------------
# Negative signal detection — nearby exclusion language disqualifies what
# would otherwise be a live lead (e.g. a variance that was actually denied).
# ---------------------------------------------------------------------------
EXCLUSION_KEYWORDS = ("excluded", "historic", "ineligible", "non-eligible", "denied")
EXCLUSION_WINDOW_CHARS = 500


def _is_excluded(
    text: str, anchor: int, bounds: tuple[int, int], window: int = EXCLUSION_WINDOW_CHARS
) -> bool:
    span_start, span_end = bounds
    start = max(span_start, anchor - window)
    end = min(span_end, anchor + window)
    window_text = text[start:end].lower()
    return any(keyword in window_text for keyword in EXCLUSION_KEYWORDS)


# ---------------------------------------------------------------------------
# Document classification — a single-parcel application vs. a citywide
# policy/ordinance packet that happens to reference multiple parcels.
# ---------------------------------------------------------------------------
_POLICY_ORDINANCE_KEYWORDS = (
    "zoning text amendment",
    "municipal code",
    "ordinance no",
    "citywide",
)


def _classify_document(
    full_text: str, signals: list[ExtractedParcelSignal]
) -> DocumentClassification:
    distinct_apns = {signal.apn for signal in signals if signal.apn}
    lowered = full_text.lower()
    is_policy = len(distinct_apns) > 1 or any(kw in lowered for kw in _POLICY_ORDINANCE_KEYWORDS)
    return "POLICY_ORDINANCE" if is_policy else "SINGLE_SITE_APPLICATION"


# ---------------------------------------------------------------------------
# Fallback policy extraction — a policy/ordinance packet's operative text is
# often just a resolution referring out to its exhibits, with the actual
# parcel list sitting in an Exhibit/Attachment APN table that has no nearby
# rezone/variance/etc. keyword to trigger the normal per-keyword scan at
# all. When that leaves us with zero signals, parse those tables directly:
# every APN inside an exhibit's span becomes its own signal, disposed
# EXCLUDED or MED based on that exhibit's own header wording.
# ---------------------------------------------------------------------------
_EXCLUSION_HEADER_HINTS = ("exclud", "denied", "withdrawn", "removed", "ineligible", "non-eligible")
_EXHIBIT_HEADER_CONTEXT_CHARS = 120


def _classify_exhibit_disposition(header_line: str) -> SignalStrength:
    lowered = header_line.lower()
    if any(hint in lowered for hint in _EXCLUSION_HEADER_HINTS):
        return SignalStrength.EXCLUDED
    return SignalStrength.MED


# Fallback-sourced signals (exhibit tables, global sweep) have no adjacent
# "X to Y" zoning sentence for ZONING_TRANSITION_PATTERN to find, so left
# alone they'd show as "Unknown" zoning in the dashboard. Infer a
# best-effort from/to zoning instead, from the document's overall policy
# shift (SB 79 upzoning) or the specific exhibit's own title.
_SB79_PATTERN = re.compile(r"\bSB[\s-]?79\b", re.IGNORECASE)


def _infer_policy_zoning(
    title: str, full_text: str, disposition: SignalStrength
) -> tuple[str | None, str | None]:
    if disposition == SignalStrength.EXCLUDED:
        return "Current Zoning", "Excluded/Unchanged"
    if _SB79_PATTERN.search(title) or _SB79_PATTERN.search(full_text):
        return "Baseline Zoning", "SB 79 TOD"
    return "Baseline Zoning", title[:80] if title else "Policy Rezoning"


def _find_exhibit_sections(full_text: str) -> list[tuple[int, int, str, str]]:
    """Returns (start, end, header_line, title) for each Exhibit/Attachment
    found in the document, spanning from one header to the next (or end of
    text). `header_line` is flattened context used for disposition keyword
    checks (e.g. spotting "excluded" anywhere nearby); `title` is just the
    descriptive line right after the header label itself (e.g. "Sites
    Temporarily Excluded from the Rezoning Program"), used as zoning
    context so it doesn't drag in subsequent table rows."""
    markers = list(_EXHIBIT_HEADER_PATTERN.finditer(full_text))
    spans: list[tuple[int, int, str, str]] = []
    for i, marker in enumerate(markers):
        start = marker.start()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(full_text)
        context_end = min(marker.end() + _EXHIBIT_HEADER_CONTEXT_CHARS, end)
        header_line = full_text[start:context_end].replace("\n", " ")
        title = full_text[marker.end() : context_end].strip().split("\n", 1)[0].strip()
        spans.append((start, end, header_line, title))
    return spans


def _page_number_at(offset: int, page_boundaries: list[tuple[int, int]]) -> int:
    page_number = page_boundaries[0][1] if page_boundaries else 1
    for start, number in page_boundaries:
        if start > offset:
            break
        page_number = number
    return page_number


def _page_info_at(
    offset: int, page_boundaries: list[tuple[int, int]], text_len: int
) -> tuple[int, int, int]:
    """Returns (page_number, page_start, page_end) for the page containing
    `offset`, used by the global sweep below to bound its per-page
    exclusion-keyword check ("surrounding page/section text")."""
    if not page_boundaries:
        return 1, 0, text_len
    for i, (start, number) in enumerate(page_boundaries):
        end = page_boundaries[i + 1][0] if i + 1 < len(page_boundaries) else text_len
        if start <= offset < end:
            return number, start, end
    last_start, last_number = page_boundaries[-1]
    return last_number, last_start, text_len


def _extract_apns_from_exhibits(
    full_text: str, city_name: str | None, page_boundaries: list[tuple[int, int]]
) -> list[ExtractedParcelSignal]:
    signals: list[ExtractedParcelSignal] = []

    for start, end, header_line, title in _find_exhibit_sections(full_text):
        disposition = _classify_exhibit_disposition(header_line)
        label_match = _EXHIBIT_HEADER_PATTERN.match(header_line)
        label = label_match.group(0) if label_match else "exhibit"
        current_zoning, proposed_zoning = _infer_policy_zoning(title, full_text, disposition)
        for apn_match in APN_PATTERN.finditer(full_text, start, end):
            signals.append(
                ExtractedParcelSignal(
                    apn=apn_match.group(1),
                    matched_keyword=f"{label} listing",
                    signal_strength=disposition,
                    extracted_text_snippet=_build_snippet(
                        full_text, apn_match.start(), apn_match.end()
                    ),
                    page_number=_page_number_at(apn_match.start(), page_boundaries),
                    current_zoning=current_zoning,
                    proposed_zoning=proposed_zoning,
                    address=_find_address(full_text, apn_match.start(), (start, end)),
                    city=city_name,
                )
            )

    return signals


# Broader APN shape than the strict San Mateo 3-3-3 format, e.g. a 2-digit
# middle group or a 4-digit final group as sometimes seen in OCR'd or
# differently-formatted tables. Last-resort tier only — see
# _global_apn_sweep.
_BROAD_APN_PATTERN = re.compile(r"\b(\d{3}-\d{2,3}-\d{3,4})\b")


def _global_apn_sweep(
    full_text: str, city_name: str | None, page_boundaries: list[tuple[int, int]]
) -> list[ExtractedParcelSignal]:
    """Last-resort fallback for POLICY_ORDINANCE documents where even the
    exhibit-section parser finds nothing (e.g. no recognizable
    Exhibit/Attachment header at all). Sweeps the entire document for any
    APN-shaped text and classifies each by whether its own page contains
    exclusion language, since there's no exhibit-specific header to key off
    of at this point."""
    signals: list[ExtractedParcelSignal] = []
    seen_spans: set[tuple[int, int]] = set()
    text_len = len(full_text)

    for pattern in (APN_PATTERN, _BROAD_APN_PATTERN):
        for match in pattern.finditer(full_text):
            if match.span() in seen_spans:
                continue
            seen_spans.add(match.span())

            page_number, page_start, page_end = _page_info_at(
                match.start(), page_boundaries, text_len
            )
            page_text = full_text[page_start:page_end].lower()
            disposition = (
                SignalStrength.EXCLUDED
                if any(keyword in page_text for keyword in EXCLUSION_KEYWORDS)
                else SignalStrength.MED
            )
            current_zoning, proposed_zoning = _infer_policy_zoning("", full_text, disposition)

            signals.append(
                ExtractedParcelSignal(
                    apn=match.group(1),
                    matched_keyword="global APN sweep",
                    signal_strength=disposition,
                    extracted_text_snippet=_build_snippet(full_text, match.start(), match.end()),
                    page_number=page_number,
                    current_zoning=current_zoning,
                    proposed_zoning=proposed_zoning,
                    address=_find_address(full_text, match.start(), (page_start, page_end)),
                    city=city_name,
                )
            )

    return signals


def _build_snippet(text: str, match_start: int, match_end: int) -> str:
    start = max(0, match_start - SNIPPET_RADIUS_CHARS)
    end = min(len(text), match_end + SNIPPET_RADIUS_CHARS)
    return text[start:end].replace("\n", " ").strip()


def _closest_match(
    pattern: re.Pattern[str], text: str, anchor: int, span_start: int, span_end: int
) -> re.Match[str] | None:
    nearest: re.Match[str] | None = None
    nearest_distance: int | None = None
    for match in pattern.finditer(text, span_start, span_end):
        distance = abs(match.start() - anchor)
        if nearest_distance is None or distance < nearest_distance:
            nearest = match
            nearest_distance = distance
    return nearest


def _nearest_match(
    pattern: re.Pattern[str],
    text: str,
    anchor: int,
    bounds: tuple[int, int],
    window: int = SEARCH_WINDOW_CHARS,
) -> re.Match[str] | None:
    """Finds the pattern match closest to `anchor`, never outside `bounds`
    (the current agenda item's span). Tries within `window` characters
    either side of the anchor first; if nothing turns up there, falls back
    to scanning the rest of the item so a distant-but-real match still
    beats no match at all."""
    span_start, span_end = bounds
    windowed = _closest_match(
        pattern, text, anchor, max(span_start, anchor - window), min(span_end, anchor + window)
    )
    if windowed is not None:
        return windowed
    return _closest_match(pattern, text, anchor, span_start, span_end)


def _find_address(text: str, anchor: int, bounds: tuple[int, int]) -> str | None:
    match = _nearest_match(ADDRESS_PATTERN, text, anchor, bounds)
    if match is None:
        match = _nearest_match(LABELED_ADDRESS_PATTERN, text, anchor, bounds)
    return match.group(1).strip() if match else None


def extract_signals_from_page(
    text: str, page_number: int, carry_in_section: str = _SECTION_UNKNOWN
) -> tuple[list[ExtractedParcelSignal], str]:
    """Scans one page's text for signals, returning them alongside the
    section kind active at the end of the page (to carry into the next
    page's call, since a hearing item's text may not repeat its header)."""
    spans, carry_out_section = _section_spans(text, carry_in_section)
    eligible = _eligible_spans(spans)

    signals: list[ExtractedParcelSignal] = []
    lowered = text.lower()

    for keyword, strength in KEYWORD_SIGNAL_STRENGTH.items():
        for match in re.finditer(re.escape(keyword), lowered):
            if not _position_in_spans(match.start(), eligible):
                continue

            item_bounds = _item_span(text, match.start())
            apn_match = _nearest_match(APN_PATTERN, text, match.start(), item_bounds)

            if apn_match:
                resolved_apn: str | None = apn_match.group(1)
                anchor = apn_match.start()
                resolved_address = _find_address(text, anchor, item_bounds)
            else:
                # No explicit APN in this item — fall back to a tightly
                # gated address-anchored lead instead of leaving apn=None.
                # See _extract_address_anchored_lead for the guardrails.
                anchor = match.start()
                resolved_apn, resolved_address = _extract_address_anchored_lead(
                    text, item_bounds
                )

            # Zoning transitions are looked up around the APN (when one was
            # found) rather than the keyword, since that's where the
            # parcel-specific detail actually sits in agenda text.
            # Everything stays within `item_bounds` so a neighboring agenda
            # item's detail can't bleed onto this one.
            zoning_match = _nearest_match(ZONING_TRANSITION_PATTERN, text, anchor, item_bounds)

            effective_strength = (
                SignalStrength.EXCLUDED if _is_excluded(text, anchor, item_bounds) else strength
            )

            signals.append(
                ExtractedParcelSignal(
                    apn=resolved_apn,
                    matched_keyword=keyword,
                    signal_strength=effective_strength,
                    extracted_text_snippet=_build_snippet(text, match.start(), match.end()),
                    page_number=page_number,
                    current_zoning=zoning_match.group(1) if zoning_match else None,
                    proposed_zoning=zoning_match.group(2) if zoning_match else None,
                    address=resolved_address,
                )
            )

    return signals, carry_out_section


def _dedupe_by_apn(signals: list[ExtractedParcelSignal]) -> list[ExtractedParcelSignal]:
    """Collapses repeated keyword matches for the same APN into a single
    signal, so one parcel doesn't produce multiple duplicate lead rows.
    Signals with no resolved APN pass through untouched, since they can't be
    tied to a specific parcel to dedupe against."""
    best_by_apn: dict[str, ExtractedParcelSignal] = {}
    unresolved: list[ExtractedParcelSignal] = []

    for signal in signals:
        if signal.apn is None:
            unresolved.append(signal)
            continue

        existing = best_by_apn.get(signal.apn)
        if existing is None:
            best_by_apn[signal.apn] = signal
            continue

        is_stronger = (
            _SIGNAL_STRENGTH_RANK[signal.signal_strength]
            > _SIGNAL_STRENGTH_RANK[existing.signal_strength]
        )
        winner, loser = (signal, existing) if is_stronger else (existing, signal)

        # Keep the strongest match as the canonical row, but backfill any
        # zoning/address/city/unit/entitlement detail it's missing from the
        # weaker duplicate.
        best_by_apn[signal.apn] = winner.model_copy(
            update={
                "current_zoning": winner.current_zoning or loser.current_zoning,
                "proposed_zoning": winner.proposed_zoning or loser.proposed_zoning,
                "address": winner.address or loser.address,
                "city": winner.city or loser.city,
                "unit_count": winner.unit_count or loser.unit_count,
                "entitlement_type": winner.entitlement_type or loser.entitlement_type,
            }
        )

    return [*best_by_apn.values(), *unresolved]


def _ocr_page(doc: "fitz.Document", page_number: int) -> str:
    """OCR fallback for a page with no extractable text layer at all
    (typically a scanned/image-only page). Renders just that one page to a
    raster image via PyMuPDF's own page.get_pixmap() — no separate poppler
    process/full-document render needed — then runs Tesseract over it.
    The Pixmap is explicitly freed and garbage-collected right after use so
    a 40+ MB, image-heavy PDF's RAM footprint doesn't creep up page by page
    over a long OCR run."""
    page = doc[page_number - 1]  # fitz pages are 0-indexed; callers use 1-based page_number
    try:
        pix = page.get_pixmap()
    except Exception:
        logger.exception("OCR page rendering failed for page %d", page_number)
        return ""

    try:
        image = pix.pil_image()
        try:
            return pytesseract.image_to_string(image) or ""
        except Exception:
            logger.exception(
                "Tesseract OCR failed for page %d (is tesseract installed? "
                "e.g. `brew install tesseract`)",
                page_number,
            )
            return ""
    finally:
        del pix
        gc.collect()
        logger.info("[mem] after OCR page %d: %.1f MB", page_number, peak_rss_mb())


def _extract_pdf_text(
    pdf_path: str,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> tuple[list[str], list[tuple[int, int]], str]:
    """Extracts per-page text from a PDF on disk — PyMuPDF (fitz) opens
    `pdf_path` directly and streams each page's text without ever loading
    the whole document into memory at once, same as the OCR fallback
    below. Returns (pages_text, page_boundaries, full_text):
    `page_boundaries` pairs each page's start offset within `full_text`
    with its 1-based page number, mirroring how the pages were joined
    below.

    Falls back to OCR for any page with no extractable text layer (a
    scanned/image-only page), so the two-stage Gemini pipeline — and the
    regex fallback — always have real words to evaluate instead of silently
    losing that page's content entirely. Only ever extracts text: images,
    site plans, and other graphics on the page are never read or sent
    anywhere — see extract_signals_from_pdf's docstring."""
    pages_text: list[str] = []
    page_boundaries: list[tuple[int, int]] = []
    offset = 0

    with fitz.open(pdf_path) as doc:
        total_pages = doc.page_count
        for page_number in range(1, total_pages + 1):
            _check_cancelled(should_cancel)
            text = doc[page_number - 1].get_text("text") or ""
            if not text.strip():
                logger.info(
                    "Page %d had no extractable text via PyMuPDF — falling back to OCR "
                    "(likely a scanned/image-only page).",
                    page_number,
                )
                _report_progress(
                    progress_callback, f"Running OCR: Page {page_number} of {total_pages}"
                )
                text = _ocr_page(doc, page_number)
                if not text.strip():
                    logger.warning(
                        "OCR also produced no text for page %d — skipping this page.",
                        page_number,
                    )
                    continue

            page_boundaries.append((offset, page_number))
            pages_text.append(text)
            offset += len(text) + 1  # +1 for the "\n" join below

    full_text = "\n".join(pages_text)
    logger.info(
        "[mem] after text extraction (%d pages, %d chars): %.1f MB",
        len(pages_text),
        len(full_text),
        peak_rss_mb(),
    )
    return pages_text, page_boundaries, full_text


def _has_resolved_apn(candidates: list[ExtractedParcelSignal]) -> bool:
    return any(signal.apn is not None for signal in candidates)


def _extract_via_regex(
    pages_text: list[str],
    page_boundaries: list[tuple[int, int]],
    full_text: str,
    city_name: str | None,
) -> tuple[list[ExtractedParcelSignal], DocumentClassification]:
    """Deterministic regex/heuristic extraction pipeline — the fallback
    used when GEMINI_API_KEY isn't set or a Gemini call fails. Fully
    self-contained and offline, so local dev and tests never depend on a
    live API call. `city_name` (the uploader's city selection) is only used
    by the exhibit-table fallback tier, since those entries rarely carry
    their own city name."""
    signals: list[ExtractedParcelSignal] = []
    section_kind = _SECTION_UNKNOWN

    for (_, page_number), text in zip(page_boundaries, pages_text):
        page_signals, section_kind = extract_signals_from_page(text, page_number, section_kind)
        signals.extend(page_signals)

    deduped = _dedupe_by_apn(signals)
    doc_type = _classify_document(full_text, deduped)

    # Gate on "no signal with a resolved APN" rather than "zero signals of
    # any kind": a long ordinance's narrative preamble routinely contains a
    # keyword like "rezoning" with no parcel number anywhere nearby (e.g.
    # "...implement the Housing Element Rezoning Program..."), which the
    # normal per-keyword pass turns into a single useless apn=None signal.
    # Gating on emptiness alone would let that one junk signal block both
    # fallback tiers below from ever running.
    if doc_type == "POLICY_ORDINANCE" and not _has_resolved_apn(deduped):
        # Likely because the parcel list lives in an exhibit table with no
        # keyword nearby each APN. Parse those tables directly instead.
        fallback_signals = _extract_apns_from_exhibits(full_text, city_name, page_boundaries)
        deduped = _dedupe_by_apn(fallback_signals)

    if doc_type == "POLICY_ORDINANCE" and not _has_resolved_apn(deduped):
        # Still nothing — the document doesn't even have a recognizable
        # Exhibit/Attachment header. Last resort: sweep the whole document
        # for anything APN-shaped rather than return empty-handed.
        fallback_signals = _global_apn_sweep(full_text, city_name, page_boundaries)
        deduped = _dedupe_by_apn(fallback_signals)

    return deduped, doc_type


# ---------------------------------------------------------------------------
# Gemini structured extraction — the primary pipeline, as a two-stage
# reasoning architecture.
#
# CEQA environmental noise and macro-plan mentions (Precise Plan, General
# Plan, etc.) can drown out the actual project intent if classification and
# extraction happen in the same call over the same large input — exactly
# what was happening on the 333 Main Street and 67-page packets. So the two
# concerns are split into separate calls with separate, narrower inputs:
#
#   Stage 1 — Intent Isolation (_classify_document_intent): one
#   classification-only call over just the absolute front matter (pages
#   1-INTENT_FRONT_MATTER_PAGE_LIMIT, the Staff Report/Executive Summary).
#   Its result — doc_type and a 2-sentence executive_summary — is locked in
#   and never revisited.
#
#   Stage 2 — Anchored Extraction (_build_extraction_prompt +
#   _call_gemini): the locked doc_type/executive_summary are injected into
#   the extraction prompt, with dynamic guidance depending on which type was
#   locked. One call covers the primary chunk (pages
#   1-FRONT_MATTER_PAGE_LIMIT); for POLICY_ORDINANCE documents the
#   remaining pages are additionally split into EXHIBIT_CHUNK_PAGE_COUNT-page
#   chunks, each extracted with its own call, so no single call ever has to
#   describe more than one chunk's worth of parcels. Stage 2's response
#   schema (_GeminiChunkResult) has no doc_type field at all, so it's
#   structurally impossible for this stage to alter what Stage 1 decided.
#
# Every call also caps max_output_tokens explicitly and checks
# finish_reason, so a truncated response is logged clearly instead of
# failing an opaque JSON-parse error with no context, and every raw result
# is logged before any aggregation or EXCLUDED-filtering happens.
# ---------------------------------------------------------------------------
# "gemini-2.5-flash" (a pinned version) returns a 404 for this API key —
# "no longer available to new users" — so this uses Google's rolling
# "-latest" alias instead, which always points at the current recommended
# flash-tier model and avoids this same breakage recurring on the next
# deprecation cycle.
GEMINI_MODEL = "gemini-flash-latest"

# Native retry for a transient Gemini 503 (the model reporting temporary
# "high demand" — confirmed live, and confirmed to still reach this code
# even after google-genai's own internal retry already gave up). Deliberately
# plain time.sleep()-based rather than pulling in tenacity or another
# dependency for just this one case. Backoff is 2s, 4s, 8s between the 4
# attempts; the 4th attempt's own failure is never slept on, since there's
# no further attempt left to wait for.
_GEMINI_503_MAX_ATTEMPTS = 4
_GEMINI_503_BACKOFF_BASE_SECONDS = 2

# Two-stage reasoning: a dedicated Stage 1 call classifies intent from just
# the absolute front matter (the Staff Report / Executive Summary, typically
# pages 1-10) with nothing else to distract it, before Stage 2 ever touches
# extraction. This is deliberately narrower than FRONT_MATTER_PAGE_LIMIT
# below — the smaller the input, the less CEQA/plan-area noise there is to
# get confused by when the only job is picking a doc_type.
INTENT_FRONT_MATTER_PAGE_LIMIT = 10

# Staff reports put the specific project/applicant/address details in the
# first ~15-20 pages; a CEQA appendix or exhibit set that follows can run
# 80+ pages. Kept small enough that a single call over just this chunk
# can't hit output-token limits. Used by Stage 2 to bound the "primary"
# extraction chunk (see _extract_via_gemini) — Stage 1 above uses its own,
# narrower window for classification only.
FRONT_MATTER_PAGE_LIMIT = 20

# Page count per supplementary chunk. Small enough that even a dense APN
# table spread across a chunk stays well within one call's output budget.
EXHIBIT_CHUNK_PAGE_COUNT = 12

# Belt-and-suspenders alongside chunking: told to the model directly, and
# also enforced as a hard ceiling via max_output_tokens below.
MAX_LEADS_PER_CALL = 40
GEMINI_MAX_OUTPUT_TOKENS = 8192

_GEMINI_SIGNAL_TO_SIGNAL_STRENGTH: dict[str, SignalStrength] = {
    "HIGH": SignalStrength.HIGH,
    "MEDIUM": SignalStrength.MED,
    "EXCLUDED": SignalStrength.EXCLUDED,
    "LOW": SignalStrength.LOW,
}

_GEMINI_EXTRACTION_RULES = f"""Look for TWO distinct kinds of lead, and set `lead_type` accordingly
for every lead you report:

- SITE_SPECIFIC: an entitlement tied to one specific parcel — a rezone, permit, variance, density
  bonus, or similar action for a named property or APN.
- POLICY_AMENDMENT: a citywide or district-wide LAND USE change with no single subject parcel —
  a General Plan update, a Title XX/zoning code text amendment, a Housing Element update, an SB 79
  transit-oriented overlay, a building/construction code amendment, or the creation of new zoning
  districts (e.g. "this ordinance establishes four new high-density zoning districts consistent
  with General Plan 2040"). These are just as important to report as a SITE_SPECIFIC lead — do
  not skip a real land-use policy change just because it has no APN attached.

  This tool tracks real estate development signals, not general civic administration. ONLY
  report a POLICY_AMENDMENT if it strictly concerns land use, zoning, housing elements,
  building/construction codes, or real estate development. Before reporting one, ask: does this
  change what can be built, where, or how much? If the answer is no, it is NEVER a lead — no
  matter how "citywide" or officially it's described, and regardless of how much agenda
  discussion time it received.

Ignore, and never report as a lead of either type:
- Civic, administrative, recreational, or other non-land-use municipal policies — e.g. a park
  bench dedication program, a dog park or off-leash area rules update, a noise ordinance
  unrelated to land use, a committee/commission merger or reorganization, a contract or vendor
  award, a fee schedule update, a special event permit, or any other parks-and-recreation program
  change. These are real municipal actions, but they are never real estate development leads —
  do not report them as a POLICY_AMENDMENT (or any lead) regardless of how the agenda item is
  titled.
- City Hall's own address (e.g. "1017 Middlefield Rd") — it is never a subject parcel.
- Applicant/consultant/law-firm office addresses found in letterheads or footers.
- Procedural boilerplate (roll call, minutes approval, adjournment, pledge of allegiance).
- Public notice radius lists — addresses of neighboring parcels notified of a hearing, not the
  parcel actually under consideration.
- Page footers/headers: page numbers, document control codes, "Page X of Y", printed/revision
  dates, and agenda packet file names — these are pagination artifacts, never project detail.
- Staff/directory listings: the City Clerk, Council/Commission roster, department contact list,
  or any "For more information, contact..." block. A phone number in this kind of listing (e.g.
  "(650) 555-1234" or "650-555-1234") is a CONTACT NUMBER, never an APN, unit count, or any other
  lead field — do not extract it into any field under any circumstances.
- Generic city boilerplate: mission statements, ADA/accessibility notices, meeting-procedure
  instructions ("public comment is limited to 3 minutes"), and standard agenda cover-page text
  that repeats on every packet regardless of what's being heard that day.

For a SITE_SPECIFIC lead:
- Use the parcel's Assessor Parcel Number (APN) as the identifier, but only if it is printed in
  a strictly numeric format like "XXX-XXX-XXX" or "XXX-XXXX-XXX". Never guess or infer the APN.
  Never put an address in the apn field — the `address` field is always where the street address
  belongs, regardless of whether an APN is present. If the exact APN is not explicitly stated in
  this text, you must return null for apn.
- Leave affected_districts null — that field is only for POLICY_AMENDMENT leads.

For a POLICY_AMENDMENT lead:
- Set apn and address to null — a macro policy change has no single parcel to attach either to.
- Populate affected_districts with the district(s), plan area(s), or scope named in the text
  (e.g. ["Downtown Precise Plan Area"], ["R-3 Zoning District"], or ["Citywide"] if the change
  applies city-wide with no named sub-area).
- Put a concise summary of the policy's actual impact (what changes, and for whom) in `reasoning`
  — this is what a reader sees in place of a parcel address, so make it stand on its own.

For every lead, of either type:
- Extract from_zoning/to_zoning if a zoning change is described, whether as a code (e.g. "R-1")
  or a descriptive name (e.g. "Low-Density Residential"). For a POLICY_AMENDMENT establishing new
  districts, use these for the new district code/name(s) where applicable.
- Extract unit_count if a specific number of housing units is mentioned.
- Extract entitlement_type (e.g. "Density Bonus", "Vesting Tentative Map", "Rezone", "Zoning Text
  Amendment").
- Extract meeting_date: the date of the city council or planning commission meeting this agenda
  is for, as printed near the top of the agenda packet (e.g. a header like "Regular Meeting —
  July 14, 2026" or "Meeting Date: 07/14/2026"). You MUST format it as a strict "YYYY-MM-DD"
  string (e.g. "2026-07-14") — never a partial date, a relative phrase ("next Tuesday"), a
  month/year only, or any other format. This is the same meeting date for every lead in the
  document — look at the document's own front matter, not language describing when the parcel's
  project itself was proposed or approved. If no meeting date is printed anywhere you've been
  shown, or you cannot express it in exactly this format, return null — never guess and never
  return a non-"YYYY-MM-DD" value.
- Set signal to EXCLUDED if the item is described as denied, withdrawn, historic, ineligible, or
  otherwise excluded from an entitlement/rezoning program rather than an active lead. Otherwise
  use HIGH for rezone/General Plan Amendment-level changes and any POLICY_AMENDMENT, MEDIUM for
  density bonus/specific plan/permit items, LOW for minor entitlements (variances, standard CUPs).
- Provide a one-sentence `reasoning` explaining why this is (or isn't) a live lead.

Report at most {MAX_LEADS_PER_CALL} leads — the most significant/active ones if there are more.
Only report leads actually described in this text — never invent a parcel or a policy change."""

# ---------------------------------------------------------------------------
# Stage 1: Intent Isolation. A dedicated, classification-only call over just
# the absolute front matter — nothing else, no extraction — so CEQA noise
# and macro-plan mentions later in the document have no chance to drown out
# the actual project intent. Its schema has no `leads` field at all, so
# there's no way for this stage to do anything but classify.
# ---------------------------------------------------------------------------
_GEMINI_INTENT_PROMPT = f"""You are a municipal zoning analyst reading ONLY the Staff Report / \
Executive Summary pages (the first {INTENT_FRONT_MATTER_PAGE_LIMIT} pages) of a city council or \
planning commission document. You have not been shown any later pages (CEQA analysis, exhibits, \
attachments) — do not assume anything about them.

Determine the primary action this document requests, and return:
- executive_summary: a 2-sentence summary of the primary action requested.
- doc_type: classified per the steps below.

Follow these steps IN ORDER — do not skip to step 2:

STEP 1 (check this FIRST): Does the document request entitlements for a specific physical
address (e.g. "333 Main Street"), name a specific applicant, reference a specific APN, or
describe specific project/site details (e.g. "Planned Community Permit", "132 residential
units", "Design Review", "Vesting Tentative Map", "Site Plan Review")? If YES to any of these,
you MUST classify doc_type as SINGLE_SITE_APPLICATION. This is true no matter how many other
signals seem to point toward a policy document — a single named applicant/project for one
address is SINGLE_SITE_APPLICATION even if:
  - it references, or must be found consistent with, an underlying Precise Plan, Specific Plan,
    or General Plan area;
  - it requests a General Plan Amendment, Zoning Text Amendment, or Precise/Specific Plan
    Amendment AS ONE OF THE ENTITLEMENTS for that one site (a site-specific land-use
    redesignation for a single parcel is still a single-site application, not a citywide one);
  - a lengthy CEQA/environmental appendix is expected to follow and repeat plan-area names
    many times — you haven't been shown it, and it wouldn't change this classification anyway.
Base doc_type on the primary requested action. Ignore background regulatory context like
"Precise Plan" or "General Plan" mentioned in the summary — do NOT classify as POLICY_ORDINANCE
simply because the document references one as part of a development application for one site.

STEP 2 (only reached if step 1 found no single-site indicator): Classify as POLICY_ORDINANCE
only if the document itself proposes a citywide or district-wide zoning code amendment, an
SB 79 transit-oriented overlay map, or another macro rezoning action with no single primary
applicant/project."""


class _GeminiIntentResult(BaseModel):
    executive_summary: str
    doc_type: DocumentClassification


def _classify_document_intent(client, front_matter_text: str) -> _GeminiIntentResult:
    """Stage 1 — classifies doc_type and summarizes intent from the
    absolute front matter alone. See _GEMINI_INTENT_PROMPT.

    The front matter is explicitly interpolated into a clearly delimited
    user-content payload here (rather than handed to the model as a bare,
    unlabeled string) so there's no ambiguity about whether the actual
    document text made it into the request — passing an empty/near-empty
    string previously surfaced as Gemini flatly replying "No document text
    was provided for evaluation" instead of a real classification."""
    if not front_matter_text.strip():
        logger.warning(
            "Stage 1 intent classification has no front-matter text to evaluate — the PDF's "
            "first %d page(s) produced no extractable text (possibly a scanned/image-only "
            "document with no OCR text layer).",
            INTENT_FRONT_MATTER_PAGE_LIMIT,
        )

    contents = (
        "Below is the document text to evaluate, delimited by === markers. Read it and follow "
        "the instructions above.\n\n"
        "=== DOCUMENT TEXT START ===\n"
        f"{front_matter_text}\n"
        "=== DOCUMENT TEXT END ==="
    )
    return _call_gemini(client, contents, _GEMINI_INTENT_PROMPT, _GeminiIntentResult)


# ---------------------------------------------------------------------------
# Stage 2: Anchored Extraction. doc_type is already locked in by Stage 1 by
# the time any of this runs — this stage's response schema has no doc_type
# field at all, so there's no way for it to alter what Stage 1 decided. The
# locked doc_type and executive_summary are injected into the prompt so
# every extraction call (primary chunk and any supplementary chunks) shares
# the same, already-settled understanding of what this document is.
# ---------------------------------------------------------------------------
def _build_extraction_prompt(doc_type: DocumentClassification, executive_summary: str) -> str:
    if doc_type == "SINGLE_SITE_APPLICATION":
        intent_instruction = (
            "The document intent is locked as a single-site project. Find and extract the "
            "primary subject property and its specific entitlements. Ignore broad regulatory "
            "plans and CEQA appendices."
        )
    else:
        intent_instruction = (
            "The document intent is locked as a macro policy. Report the policy change(s) "
            "themselves as POLICY_AMENDMENT leads (see below) — do not skip these just because "
            "they lack an APN. Separately, scan the chunks for any actively affected individual "
            "parcels (e.g. an attached exhibit table) and report those as SITE_SPECIFIC leads, "
            "ignoring exempted/historic properties."
        )

    return f"""You are a municipal zoning analyst reviewing one excerpt (a range of consecutive \
pages) from a larger city council or planning commission document. A prior analysis pass has \
already determined this document's overall intent — do not re-derive or contradict it:
- Executive summary: {executive_summary}
- Locked doc_type: {doc_type}
{intent_instruction}

Extract every real estate development lead described in THIS excerpt only — do not reference or \
assume content from other parts of the document you have not been shown.

{_GEMINI_EXTRACTION_RULES}"""


# Enforces the "YYYY-MM-DD" format required of _GeminiLeadItem.meeting_date
# (see its field validator below) — a strict 4-2-2 digit shape, not just
# "contains a date somewhere," since rezoning_leads.meeting_date is a real
# Postgres `date` column that would reject anything looser.
_STRICT_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _GeminiLeadItem(BaseModel):
    lead_type: Literal["SITE_SPECIFIC", "POLICY_AMENDMENT"]
    address: str | None = None
    apn: str | None = Field(
        default=None,
        description=(
            "The Assessor's Parcel Number (APN). Must strictly follow numeric formats like "
            "'XXX-XXX-XXX' or 'XXX-XXXX-XXX'. Do NOT substitute an address, and NEVER a phone "
            "number (e.g. '650-555-1234' is a contact number, not an APN, even though it is "
            "also digits and hyphens) — a real APN's final group is always exactly 3 digits, "
            "never 4. If a valid numeric APN is not explicitly stated in the document, you must "
            "return null. Always null for a POLICY_AMENDMENT lead — a macro policy change has "
            "no single parcel."
        ),
    )
    # Only ever populated for a POLICY_AMENDMENT lead — the district(s),
    # plan area(s), or scope of a macro zoning change (e.g. ["Downtown
    # Precise Plan Area"] or ["Citywide"]). Always null for SITE_SPECIFIC.
    affected_districts: list[str] | None = Field(
        default=None,
        description=(
            "For a POLICY_AMENDMENT lead only: the district(s), plan area(s), or scope this "
            "policy change affects (e.g. ['Downtown Precise Plan Area'], or ['Citywide'] if it "
            "applies city-wide with no named sub-area). Always null for a SITE_SPECIFIC lead."
        ),
    )
    from_zoning: str | None = None
    to_zoning: str | None = None
    unit_count: int | None = None
    entitlement_type: str | None = None
    meeting_date: str | None = Field(
        default=None,
        description=(
            "The exact date of the city council or planning commission meeting found on the "
            "agenda document. You MUST format this as a strict 'YYYY-MM-DD' string (e.g. "
            "'2026-07-14') — never a partial date, a relative phrase, or any other date format. "
            "If the exact date is not found, or cannot be expressed in exactly this format, you "
            "must return null."
        ),
    )
    signal: Literal["HIGH", "MEDIUM", "EXCLUDED", "LOW"]
    reasoning: str

    @field_validator("apn", mode="before")
    @classmethod
    def _reject_non_numeric_apn(cls, value: str | None) -> str | None:
        """Belt-and-suspenders alongside the field description and prompt
        constraints above: Gemini has been observed hallucinating an
        address into this field (e.g. "ADDR: 320 Sheridan Dr.") when no
        real APN is printed, despite being told not to — and, in
        production, a City Clerk/staff phone number (e.g. "650-555-1234")
        has been observed coming through here too. An "only digits and
        hyphens" check alone doesn't catch that: a phone number contains
        no letters, so it would pass a bare alpha check right through.
        This instead requires the value to fully match one of the two
        real APN shapes (see GEMINI_APN_SHAPE_PATTERN) — a phone number's
        3-3-4 digit grouping matches neither, so it gets nulled out here
        rather than trusting the model's own restraint."""
        if value is None:
            return None
        candidate = value.strip()
        if not GEMINI_APN_SHAPE_PATTERN.match(candidate):
            return None
        return candidate

    @field_validator("meeting_date", mode="before")
    @classmethod
    def _reject_malformed_meeting_date(cls, value: str | None) -> str | None:
        """Belt-and-suspenders alongside the field description and prompt
        constraints above: despite being told the exact required format,
        Gemini can still return a relative phrase, a partial date, or some
        other non-"YYYY-MM-DD" string. rezoning_leads.meeting_date is a
        strict Postgres `date` column (see database/schema.sql), so a
        malformed value reaching the database would raise a casting error
        — nulling it out here instead means the ingest persistence layer
        can always fall back to the parent document's meeting_date (see
        routers/ingest.py's _persist_to_supabase) rather than ever passing
        something uncastable through."""
        if value is None:
            return None
        if not _STRICT_ISO_DATE_PATTERN.match(value):
            return None
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return value


class _GeminiChunkResult(BaseModel):
    """Stage 2's response shape. Deliberately has no doc_type field —
    Stage 1 already locked that in, and this schema makes it structurally
    impossible for Stage 2 to report a different one."""

    leads: list[_GeminiLeadItem] = Field(default_factory=list)


def _has_required_fields(item: _GeminiLeadItem) -> bool:
    """A lead missing either field is treated as too low-quality to
    persist — usually a hallucinated match with no real meeting context or
    named entitlement behind it (e.g. a stray keyword match against
    boilerplate). Applied only to Gemini-extracted leads (see
    _extract_via_gemini's caller) — the regex/heuristic fallback pipeline
    never populates entitlement_type at all, so applying this gate there
    would silently drop every single fallback-pipeline lead."""
    has_meeting_date = bool(item.meeting_date)
    has_entitlement_type = bool(item.entitlement_type and item.entitlement_type.strip())
    return has_meeting_date and has_entitlement_type


def is_gemini_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _locate_page(
    needle: str | None, full_text: str, page_boundaries: list[tuple[int, int]]
) -> int:
    """Approximates which page a Gemini-reported address/APN came from by
    searching for it in the full concatenated document text. Gemini's
    schema doesn't report page numbers itself, so this is best-effort, and
    works the same regardless of which call (front-matter or a chunk)
    produced the lead."""
    if needle:
        pos = full_text.find(needle)
        if pos != -1:
            return _page_number_at(pos, page_boundaries)
    return page_boundaries[0][1] if page_boundaries else 1


def _chunk_pages(
    pages_text: list[str],
    page_boundaries: list[tuple[int, int]],
    start_index: int,
    chunk_page_count: int,
) -> list[str]:
    """Groups pages[start_index:] into chunk_page_count-page text blocks,
    each labeled with its page range, so no single Gemini call has to
    describe more parcels than fit in one modest chunk."""
    items = list(zip(pages_text[start_index:], page_boundaries[start_index:]))
    chunks: list[str] = []
    for i in range(0, len(items), chunk_page_count):
        group = items[i : i + chunk_page_count]
        if not group:
            continue
        first_page, last_page = group[0][1][1], group[-1][1][1]
        text = "\n".join(page_text for page_text, _ in group)
        chunks.append(f"=== Pages {first_page}-{last_page} of the document ===\n{text}")
    return chunks


def _call_gemini(
    client, contents: str, system_prompt: str, response_schema: type[BaseModel]
):
    """Runs one Gemini structured-extraction call, retrying a transient 503
    (the model reporting temporary high demand — see
    _GEMINI_503_MAX_ATTEMPTS) with a native exponential backoff before
    giving up. Logs the specific API error or JSON-parse failure (including
    a snippet of the raw response and the finish_reason) instead of letting
    a large-document failure surface as an opaque, unexplained fallback to
    the regex pipeline — a 503 that exhausts every retry, any other
    APIError, or a parse failure all still re-raise exactly as before, so
    extract_signals_from_pdf's outer try/except can fall back to the regex
    pipeline unchanged."""
    from google.genai import errors, types

    if not contents.strip():
        logger.warning(
            "Gemini call about to send empty/whitespace-only contents — the model has no "
            "document text to read and will very likely reject or hallucinate a response."
        )

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=response_schema,
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )

    response = None
    for attempt in range(1, _GEMINI_503_MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config
            )
            break
        except errors.ServerError as exc:
            # ServerError covers the whole 5xx range — only a 503 (high
            # demand) is treated as transient/retryable here; any other
            # server error falls straight through to the logging+raise
            # below, same as before this retry loop existed.
            is_503 = getattr(exc, "code", None) == 503
            if not is_503 or attempt == _GEMINI_503_MAX_ATTEMPTS:
                logger.error(
                    "Gemini API call failed (%s, %d chars of input): %s: %s",
                    getattr(exc, "code", "unknown status"),
                    len(contents),
                    type(exc).__name__,
                    exc,
                )
                raise
            delay_seconds = _GEMINI_503_BACKOFF_BASE_SECONDS**attempt
            logger.warning(
                "Gemini returned 503 (high demand) on attempt %d/%d (%d chars of input) — "
                "retrying in %ds.",
                attempt,
                _GEMINI_503_MAX_ATTEMPTS,
                len(contents),
                delay_seconds,
            )
            time.sleep(delay_seconds)
        except errors.APIError as exc:
            logger.error(
                "Gemini API call failed (%s, %d chars of input): %s: %s",
                getattr(exc, "code", "unknown status"),
                len(contents),
                type(exc).__name__,
                exc,
            )
            raise

    finish_reason = response.candidates[0].finish_reason if response.candidates else None
    if finish_reason is not None and finish_reason != types.FinishReason.STOP:
        logger.warning(
            "Gemini response (%d chars of input) finished abnormally "
            "(finish_reason=%s) — output may be truncated or blocked.",
            len(contents),
            finish_reason,
        )

    result = response.parsed
    if not isinstance(result, response_schema):
        try:
            result = response_schema.model_validate_json(response.text)
        except Exception as exc:
            logger.error(
                "Failed to parse Gemini JSON response (%s): %s\n"
                "Raw response (first 2000 chars): %s",
                type(exc).__name__,
                exc,
                (response.text or "")[:2000],
            )
            raise

    # Raw, unfiltered/unaggregated result straight from this call — logged
    # before _extract_via_gemini merges chunks or extract_signals_from_pdf
    # suppresses EXCLUDED leads, so the actual model output is always
    # visible for debugging a bad classification or extraction.
    logger.info("Gemini raw extraction result: %s", result.model_dump_json())

    return result


def _extract_via_gemini(
    pages_text: list[str],
    page_boundaries: list[tuple[int, int]],
    full_text: str,
    city_name: str | None,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> tuple[list[ExtractedParcelSignal], DocumentClassification]:
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    logger.info("[mem] entering _extract_via_gemini: %.1f MB", peak_rss_mb())

    # --- Stage 1: Intent Isolation ------------------------------------
    # Classification-only call over just the absolute front matter. Its
    # result — doc_type and executive_summary — is locked in here and
    # never revisited; Stage 2 below has no schema field that could alter
    # doc_type even if it wanted to.
    _check_cancelled(should_cancel)
    _report_progress(progress_callback, "Stage 1: Checking intent...")
    intent_page_count = min(INTENT_FRONT_MATTER_PAGE_LIMIT, len(pages_text))
    intent_text = "\n".join(pages_text[:intent_page_count])
    intent = _classify_document_intent(client, intent_text)
    doc_type = intent.doc_type
    executive_summary = intent.executive_summary
    logger.info("[mem] after Stage 1 (doc_type=%s): %.1f MB", doc_type, peak_rss_mb())

    # --- Stage 2: Anchored Extraction ---------------------------------
    # Every extraction call — the primary chunk and any supplementary
    # chunks — shares one prompt built from the locked Stage 1 result.
    _check_cancelled(should_cancel)
    _report_progress(progress_callback, "Stage 2: Extracting leads...")
    extraction_prompt = _build_extraction_prompt(doc_type, executive_summary)

    front_matter_page_count = min(FRONT_MATTER_PAGE_LIMIT, len(pages_text))
    front_matter_text = "\n".join(pages_text[:front_matter_page_count])
    primary_result = _call_gemini(
        client, front_matter_text, extraction_prompt, _GeminiChunkResult
    )
    lead_items = list(primary_result.leads)
    logger.info("[mem] after Stage 2 primary chunk: %.1f MB", peak_rss_mb())

    if doc_type == "POLICY_ORDINANCE":
        chunks = _chunk_pages(
            pages_text, page_boundaries, front_matter_page_count, EXHIBIT_CHUNK_PAGE_COUNT
        )
        logger.info(
            "[mem] before supplementary chunk loop (%d chunks): %.1f MB",
            len(chunks),
            peak_rss_mb(),
        )
        for i, chunk_text in enumerate(chunks, start=1):
            # Checked outside the try/except below so a cancellation
            # propagates up instead of being logged as "this chunk failed"
            # and swallowed.
            _check_cancelled(should_cancel)
            _report_progress(
                progress_callback,
                f"Stage 2: Extracting leads (chunk {i} of {len(chunks)})...",
            )
            try:
                chunk_result = _call_gemini(
                    client, chunk_text, extraction_prompt, _GeminiChunkResult
                )
            except Exception:
                logger.exception(
                    "Gemini extraction failed for supplementary chunk %d/%d — skipping it and "
                    "continuing with the remaining chunks.",
                    i,
                    len(chunks),
                )
                continue
            logger.info(
                "[mem] after chunk %d/%d: %.1f MB", i, len(chunks), peak_rss_mb()
            )
            lead_items.extend(chunk_result.leads)

    accepted_items: list[_GeminiLeadItem] = []
    for item in lead_items:
        if not _has_required_fields(item):
            logger.info(
                "Dropping Gemini-extracted lead — missing required field(s) "
                "(meeting_date=%r, entitlement_type=%r): %s",
                item.meeting_date,
                item.entitlement_type,
                item.reasoning,
            )
            continue
        accepted_items.append(item)
    lead_items = accepted_items

    signals = [
        ExtractedParcelSignal(
            apn=item.apn,
            matched_keyword=item.entitlement_type or "gemini extraction",
            signal_strength=_GEMINI_SIGNAL_TO_SIGNAL_STRENGTH[item.signal],
            extracted_text_snippet=item.reasoning,
            page_number=_locate_page(item.address or item.apn, full_text, page_boundaries),
            current_zoning=item.from_zoning,
            proposed_zoning=item.to_zoning,
            address=item.address,
            city=city_name,
            unit_count=item.unit_count,
            entitlement_type=item.entitlement_type,
            meeting_date=item.meeting_date,
            lead_type=LeadType(item.lead_type),
            affected_districts=item.affected_districts,
        )
        for item in lead_items
    ]

    return _dedupe_by_apn(signals), doc_type


def extract_signals_from_pdf(
    pdf_path: str,
    city_name: str | None = None,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> tuple[list[ExtractedParcelSignal], DocumentClassification, int]:
    """Runs the full scan pipeline over a municipal PDF on disk (`pdf_path`
    — the caller owns that file's lifecycle, see routers/ingest.py's
    _run_ingest_job), returning the deduped signals, a document-level
    classification, and a count of EXCLUDED signals suppressed from that
    list. Uses Gemini when GEMINI_API_KEY is configured, falling back to
    the regex/heuristic pipeline if the key is missing or the Gemini call
    fails.

    Only ever sends extracted plain text to Gemini, never the PDF file
    itself or any image/graphic from it: _extract_pdf_text below reads the
    file once, up front, via PyMuPDF (OCR for any page with no text
    layer), and every Gemini call after that — see _call_gemini — takes a
    plain Python string as its payload.

    `progress_callback`, if given, is called with human-readable status
    strings ("Running OCR: Page 3 of 40", "Stage 1: Checking intent...")
    for a caller to surface live (e.g. the SSE job endpoint). `should_cancel`,
    if given, is polled between pages/chunks and raises IngestCancelled
    (propagated straight to the caller, never treated as an ordinary
    failure) so a user-initiated cancellation actually stops the run."""
    _check_cancelled(should_cancel)
    _report_progress(progress_callback, "Reading PDF pages...")
    pages_text, page_boundaries, full_text = _extract_pdf_text(
        pdf_path, progress_callback, should_cancel
    )

    if is_gemini_configured():
        try:
            signals, doc_type = _extract_via_gemini(
                pages_text, page_boundaries, full_text, city_name, progress_callback, should_cancel
            )
        except IngestCancelled:
            raise
        except Exception:
            # Chunk-level failures are already logged and skipped inside
            # _extract_via_gemini; reaching here means the front-matter
            # call itself failed, so we don't even know doc_type.
            logger.exception(
                "Gemini front-matter extraction failed; falling back to the "
                "regex/heuristic pipeline"
            )
            _report_progress(progress_callback, "Falling back to offline extraction...")
            signals, doc_type = _extract_via_regex(
                pages_text, page_boundaries, full_text, city_name
            )
    else:
        logger.warning(
            "GEMINI_API_KEY not set; using the regex/heuristic extraction pipeline instead"
        )
        _report_progress(progress_callback, "Scanning document (offline mode)...")
        signals, doc_type = _extract_via_regex(pages_text, page_boundaries, full_text, city_name)

    deduped = signals

    excluded_count = 0
    if doc_type == "POLICY_ORDINANCE":
        # A large rezoning-program ordinance can list hundreds of excluded
        # parcels — surfacing all of them as "leads" would flood the table
        # with negatives. Keep only the active ones and report how many
        # were filtered out instead, so the frontend can summarize them.
        active = [s for s in deduped if s.signal_strength != SignalStrength.EXCLUDED]
        excluded_count = len(deduped) - len(active)
        deduped = active

    return deduped, doc_type, excluded_count
