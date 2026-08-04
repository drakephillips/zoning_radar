"""Shared, portal-agnostic helpers for the municipal agenda scraper.

Every ScraperStrategy (see services/scraper.py) needs the same handful of
low-level building blocks — turning a relative link into an absolute URL,
pulling a meeting date out of whatever text is available, and presenting a
consistent, realistic set of HTTP headers — regardless of which vendor
platform it's navigating. Centralizing them here keeps each strategy
focused on its own site-specific navigation logic instead of re-deriving
these basics.
"""

import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

from dateutil import parser as dateutil_parser

logger = logging.getLogger(__name__)


def normalize_url(base_url: str, link: str) -> str:
    """Converts a possibly-relative `link` into an absolute URL relative to
    `base_url`. A thin, explicit wrapper around urljoin so every strategy
    converts links the same way, in one documented place."""
    return urljoin(base_url, link)


# Pseudo-link schemes that are never a real document, regardless of what
# their href or visible text happens to contain — e.g. Pacifica's
# "mailto:clerk@...?subject=Place me on the City Council Agenda
# Distribution List" link matches a naive "agenda" keyword check on either
# the href or the link text, even though it's an email link, not a
# document.
_REJECTED_HREF_SCHEMES = ("mailto:", "tel:", "javascript:")


def is_navigable_href(href: str) -> bool:
    """True unless `href` is a mailto:/tel:/javascript: pseudo-link. Every
    strategy must check this before running any keyword match against an
    anchor's href or visible text, so a pseudo-link can never masquerade as
    a real document just because it happens to contain a matched keyword."""
    return not href.strip().lower().startswith(_REJECTED_HREF_SCHEMES)


def get_default_headers() -> dict[str, str]:
    """Realistic browser headers for scraping public municipal agenda
    portals. A self-identifying User-Agent would be more honest, but
    several target portals run WAF/bot-protection rules (e.g. Akamai,
    Granicus's and CivicPlus's own WAFs) that block anything that doesn't
    look like a real browser, regardless of intent — these listing pages
    are public and the request volume here is a handful of GETs per run,
    so presenting as a normal, reasonably current browser is the pragmatic
    tradeoff. This is the single source of truth for outbound scraping
    headers — every curl_cffi AsyncSession the scraper constructs
    (services/scraper.py's find_new_documents, routers/scraper.py's
    download client) is built with this, and every strategy/secondary
    request reuses that same session, so there's no separate call site that
    needs its own copy of these values. Returns a fresh dict each call so
    callers can't accidentally mutate a shared instance."""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.5",
    }


# Tried first, in order, against arbitrary scraped text (a link's href, its
# visible text, or a table cell) — these cover the date encodings actually
# seen on municipal portals so far. Each pattern's groups are (year, month,
# day) except the last, which is (month, day, year).
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # A YYYYMMDD slug segment, e.g. Menlo Park's
    # ".../20260714-city-council-special-and-regular-agenda.pdf".
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
    # ISO-ish YYYY-MM-DD, in case a link text/href/table cell spells it out.
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),
    # US-style MM/DD/YYYY, common in human-readable link text.
    re.compile(r"(\d{1,2})/(\d{1,2})/(20\d{2})"),
)

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
)

# Matches just the date phrase itself (e.g. "June 23, 2026" or "Jul. 14th
# 2026") within a larger string — sorted longest-first so an alternation
# like "jun"/"june" can't short-circuit on the shorter prefix. Deliberately
# used to *isolate* the date phrase before handing it to dateutil, rather
# than fuzzy-parsing the surrounding text directly: real scraped row text
# is often "June 23, 2026 Minutes Agenda Packet Addendum", and dateutil's
# fuzzy parser mis-tokenizes "Minutes" there as a time-unit keyword and
# raises ("minute must be in 0..59") instead of just ignoring it — isolating
# the date phrase first sidesteps that entirely.
_MONTH_NAME_DATE_PATTERN = re.compile(
    r"\b(?:" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")\.?\s+"
    r"\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2}\b",
    re.IGNORECASE,
)


def parse_meeting_date(text: str) -> str | None:
    """Best-effort extraction of a meeting date from arbitrary scraped
    text — a link's URL, its visible link text, or a table cell — returned
    as an ISO "YYYY-MM-DD" string, or None if nothing plausible is found.

    Tries the fixed regex patterns above first (exact and cheap for the
    encodings already seen on real municipal portals), then looks for a
    "Month Day, Year" phrase (e.g. "July 14, 2026") anywhere in the text
    and, if found, hands *just that isolated phrase* to dateutil — never
    the surrounding text, which risks dateutil misinterpreting unrelated
    words as date/time components (see _MONTH_NAME_DATE_PATTERN)."""
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            year_str = match.group(1)
            try:
                if len(year_str) == 4:  # YYYYMMDD / YYYY-MM-DD: year first
                    year, month, day = (int(g) for g in match.groups())
                else:  # MM/DD/YYYY: year last
                    month, day, year = (int(g) for g in match.groups())
                return date(year, month, day).isoformat()
            except ValueError:
                continue  # not a real calendar date (e.g. month "13") — keep looking

    match = _MONTH_NAME_DATE_PATTERN.search(text)
    if match is None:
        return None

    try:
        parsed = dateutil_parser.parse(match.group(0), fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    return parsed.date().isoformat()


# Strict MM/DD/YY, e.g. "07/06/26" — Burlingame's Granicus skin renders
# this in a dedicated <td headers="Date"> cell (see
# GranicusStrategy._parse_date_from_headers_cell) with no 4-digit-year
# form anywhere else in the row for its older archive entries, so none of
# parse_meeting_date's patterns above ever match it.
_SHORT_YEAR_DATE_PATTERN = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")


def parse_short_year_date(text: str) -> str | None:
    """Parses a strict "MM/DD/YY" string (e.g. "07/06/26") into an ISO
    "YYYY-MM-DD" string, or None if `text` isn't in exactly that shape or
    isn't a real calendar date. Two-digit years follow Python's own
    strptime "%y" windowing (00-68 -> 2000-2068, 69-99 -> 1969-1999),
    which is correct for the near-term municipal meeting dates this
    covers. Deliberately strict (anchored, no surrounding text) since this
    exists specifically to feed a Postgres `date` column — see
    rezoning_leads.meeting_date in database/schema.sql."""
    stripped = text.strip()
    if not _SHORT_YEAR_DATE_PATTERN.match(stripped):
        return None
    try:
        parsed = datetime.strptime(stripped, "%m/%d/%y")
    except ValueError:
        return None
    return parsed.date().isoformat()
