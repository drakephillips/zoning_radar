"""Scans municipal PDF agendas/minutes for rezoning signals, APNs, zoning
transitions, and nearby street addresses."""

import re
from typing import BinaryIO

import pdfplumber

from models.schemas import ExtractedParcelSignal, SignalStrength

# San Mateo County APNs are formatted as three groups of digits, e.g. 042-311-090
APN_PATTERN = re.compile(r"\b(\d{3}-\d{3}-\d{3})\b")

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
KEYWORD_SIGNAL_STRENGTH: dict[str, SignalStrength] = {
    "general plan amendment": SignalStrength.HIGH,
    "rezone": SignalStrength.HIGH,
    "rezoning": SignalStrength.HIGH,
    "density bonus": SignalStrength.MED,
    "specific plan": SignalStrength.MED,
    "zoning text amendment": SignalStrength.MED,
    "conditional use permit": SignalStrength.LOW,
    "variance": SignalStrength.LOW,
}

_SIGNAL_STRENGTH_RANK = {SignalStrength.HIGH: 3, SignalStrength.MED: 2, SignalStrength.LOW: 1}

SNIPPET_RADIUS_CHARS = 160

# How far either side of the APN to look for zoning/address detail before
# falling back to scanning the entire page.
SEARCH_WINDOW_CHARS = 1000


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
    pattern: re.Pattern[str], text: str, anchor: int, window: int = SEARCH_WINDOW_CHARS
) -> re.Match[str] | None:
    """Finds the pattern match closest to `anchor`. Tries within `window`
    characters either side of it first; if nothing turns up there, falls
    back to scanning the entire page so a distant-but-real match still beats
    no match at all."""
    windowed = _closest_match(
        pattern, text, anchor, max(0, anchor - window), min(len(text), anchor + window)
    )
    if windowed is not None:
        return windowed
    return _closest_match(pattern, text, anchor, 0, len(text))


def _find_address(text: str, anchor: int) -> str | None:
    match = _nearest_match(ADDRESS_PATTERN, text, anchor)
    if match is None:
        match = _nearest_match(LABELED_ADDRESS_PATTERN, text, anchor)
    return match.group(1).strip() if match else None


def extract_signals_from_page(text: str, page_number: int) -> list[ExtractedParcelSignal]:
    signals: list[ExtractedParcelSignal] = []
    lowered = text.lower()

    for keyword, strength in KEYWORD_SIGNAL_STRENGTH.items():
        for match in re.finditer(re.escape(keyword), lowered):
            apn_match = _nearest_match(APN_PATTERN, text, match.start())

            # Zoning transitions and addresses are looked up around the APN
            # (when one was found) rather than the keyword, since that's
            # where the parcel-specific detail actually sits in agenda text.
            anchor = apn_match.start() if apn_match else match.start()
            zoning_match = _nearest_match(ZONING_TRANSITION_PATTERN, text, anchor)

            signals.append(
                ExtractedParcelSignal(
                    apn=apn_match.group(1) if apn_match else None,
                    matched_keyword=keyword,
                    signal_strength=strength,
                    extracted_text_snippet=_build_snippet(text, match.start(), match.end()),
                    page_number=page_number,
                    current_zoning=zoning_match.group(1) if zoning_match else None,
                    proposed_zoning=zoning_match.group(2) if zoning_match else None,
                    address=_find_address(text, anchor),
                )
            )

    return signals


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
        # zoning/address detail it's missing from the weaker duplicate.
        best_by_apn[signal.apn] = winner.model_copy(
            update={
                "current_zoning": winner.current_zoning or loser.current_zoning,
                "proposed_zoning": winner.proposed_zoning or loser.proposed_zoning,
                "address": winner.address or loser.address,
            }
        )

    return [*best_by_apn.values(), *unresolved]


def extract_signals_from_pdf(file: BinaryIO) -> list[ExtractedParcelSignal]:
    """Runs the full scan pipeline over a municipal PDF's pages."""
    signals: list[ExtractedParcelSignal] = []

    with pdfplumber.open(file) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if not text:
                continue
            signals.extend(extract_signals_from_page(text, page_number))

    return _dedupe_by_apn(signals)
