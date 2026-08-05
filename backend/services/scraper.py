"""Generic municipal agenda-portal scraper — Strategy Pattern.

Fetches a jurisdiction's agenda listing page and finds public PDF links for
the high-yield bodies (City Council, Planning Commission, Board of
Supervisors, or anything zoning-related) whose URL or link text mentions
"Agenda", "Packet", or "Staff Report" — explicitly skipping irrelevant
boards/commissions (parks, library, arts, etc.) and "minutes" documents,
since those generate near-zero rezoning signal but cost the same OCR/Gemini
processing time. See RELEVANT_BODY_KEYWORDS / AGENDA_KEYWORDS /
EXCLUDED_KEYWORDS below. The documents routers/scraper.py then downloads
and feeds into the existing background ingestion pipeline
(services/pdf_parser.py via routers/ingest.py's _run_ingest_job).
Deduplicates against local_store.is_scraped_url so a previously-downloaded
PDF URL is never re-fetched or re-queued.

San Mateo County's 20-odd jurisdictions run on a handful of different
agenda-portal vendor platforms (a plain HTML listing page, Granicus/
Legistar, CivicPlus AgendaCenter, or a JSON API like CivicClerk/PrimeGov),
each requiring different navigation logic to reach the actual PDFs. This
module models that as a Strategy Pattern: ScraperStrategy is the common
interface, one concrete subclass per vendor platform, and get_strategy()
maps a MunicipalSource.type (see config/sources.py) to the right one.
Shared, portal-agnostic helpers (URL normalization, date parsing, default
headers) live in services/scraper_utils.py.

All outbound requests go through curl_cffi's AsyncSession with
impersonate="chrome" (see find_new_documents/routers/scraper.py), which
spoofs a real browser's TLS/JA3 fingerprint — several sources' WAFs
(Akamai and others) block on that fingerprint even with a fully realistic
header set, so header-only spoofing isn't sufficient on its own.

Every strategy has real scraping logic, each with vendor-specific quirks
resolved past simple happy-path parsing:
  - DirectPdfStrategy: a listing page with plain <a href="...pdf"> links.
  - GranicusStrategy: walks <tr> rows for an "agenda"-labelled link, then
    _resolve_pdf_url chases whatever chain of indirection that link
    involves — a direct .pdf/View.ashx/ShowPublishedDocument link, an
    AgendaViewer.php redirect (recursing when the redirect target is
    itself another intermediate wrapper, e.g. AgendaOnline/ViewMeeting,
    rather than trusting any single hop as terminal), a Google Docs Viewer
    embed needing its target unwrapped, an underscore-named S3 bucket
    needing path-style addressing for a valid TLS cert, or an AgendaOnline
    Documents/Downloadfile link, which is itself just a client-side
    "Downloading, please wait..." interstitial requiring a real POST to
    Documents/InvokeDownloadAttachment followed by a GET to
    Documents/ViewDocument to reach the actual PDF bytes (see
    _resolve_downloadfile_url). A fallback <td headers="Date"> cell parser
    covers rows whose only date is a two-digit-year "MM/DD/YY" string.
  - CivicPlusStrategy: matches "catAgendaRow" or IQM2/Legistar-Insight's
    "MeetingRow" row classes (falling back to a generic div/tr sweep only
    if neither is present), rejects mailto:/tel:/javascript: pseudo-links
    and notification/subscribe/signup utility pages outright, and — for
    any matched link that doesn't already end in .pdf — issues a
    lightweight HEAD request to confirm the real Content-Type before
    queuing it, since a matched link shape (ViewFile, ShowPublishedDocument,
    FileOpen.aspx) doesn't guarantee the target is actually a PDF.
  - ApiStrategy: handles CivicClerk's /v1/Events (OData-wrapped JSON,
    file entries nested under a per-tenant-named list key) and PrimeGov's
    ListArchivedMeetings (documentList entries distinguished by
    compileOutputType — 1 is PDF, 3 is HTML — resolved via
    /Public/CompiledDocument/{id}, which redirects to a real, time-limited
    signed blob URL; PrimeGov's own /Portal/viewer only serves an HTML
    Accusoft viewer wrapper, never the PDF itself).

None of this guarantees every configured jurisdiction currently returns
documents — a portal that renders its listing via client-side JS/AJAX
still yields zero rows here (logged, not a crash), and a vendor's field
names/URL shapes can differ from tenant to tenant. Verify a given source
live before depending on it in production.
"""

import abc
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import HTTPError, RequestException
from pydantic import BaseModel

import local_store
from config.sources import SOURCES as SOURCE_CONFIG
from services.scraper_utils import (
    get_default_headers,
    is_navigable_href,
    normalize_url,
    parse_meeting_date,
    parse_short_year_date,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30.0

# Sensible default window width when a caller doesn't pass explicit
# start_date/end_date bounds — e.g. a daily delta cron job should still get
# a bounded, recent-only sweep rather than accidentally pulling a source's
# entire history if it's ever invoked without an explicit window.
DEFAULT_LOOKBACK_DAYS = 30

# Only these bodies are considered high-yield for rezoning signal
# extraction — every other board/commission on a city's listing page
# (parks, library, arts, etc.) still costs the same OCR/Gemini processing
# time for near-zero relevant parcel signals.
RELEVANT_BODY_KEYWORDS = (
    "city council",
    "planning commission",
    "zoning",
    "board of supervisors",
)

# A link only counts as an agenda document if its URL or visible text also
# mentions one of these — otherwise unrelated PDFs on the same listing page
# (minutes, budgets, newsletters, forms) would get swept in too.
AGENDA_KEYWORDS = ("agenda", "packet", "staff report")

# Skipped regardless of the above — catches irrelevant boards/commissions
# whose links can otherwise still contain a RELEVANT_BODY_KEYWORDS match
# (e.g. via shared listing-page markup), plus "minutes" documents, which
# record what a body already decided rather than what's up for a vote.
EXCLUDED_KEYWORDS = (
    "parks",
    "recreation",
    "aquatics",
    "library",
    "picnic",
    "senior",
    "arts",
    "heritage",
    "minutes",
)


class MunicipalSource(BaseModel):
    name: str
    city: str
    listing_url: str
    # Which ScraperStrategy this source needs — see config/sources.py and
    # get_strategy() below.
    type: str = "direct_pdf"


def _default_source_name(jurisdiction: str) -> str:
    """"<jurisdiction> City Council" for every city/town, except the
    county itself, which has a Board of Supervisors rather than a council."""
    if jurisdiction.endswith("County"):
        return f"{jurisdiction} Board of Supervisors"
    return f"{jurisdiction} City Council"


# See the module docstring's IMPORTANT note — verify/update before
# depending on this in production. Adapted from config/sources.py's raw
# SOURCE_CONFIG: `jurisdiction` becomes both `city` and the display `name`
# (see _default_source_name), `url` becomes `listing_url`, and `type`
# carries straight through.
SOURCES: list[MunicipalSource] = [
    MunicipalSource(
        name=_default_source_name(entry["jurisdiction"]),
        city=entry["jurisdiction"],
        listing_url=entry["url"],
        type=entry["type"],
    )
    for entry in SOURCE_CONFIG
]


class ScrapedDocument(BaseModel):
    """The data contract every ScraperStrategy returns, regardless of which
    vendor platform it navigated — downstream code (routers/scraper.py)
    only ever deals with this shape, never a strategy-specific one."""

    jurisdiction: str
    pdf_url: str
    # ISO "YYYY-MM-DD", best-effort — see scraper_utils.parse_meeting_date.
    # None when no date could be found in the URL/link text/table cell.
    meeting_date: str | None = None
    title: str | None = None
    # The MunicipalSource.type that produced this document (e.g.
    # "direct_pdf") — lets downstream code/logging trace which strategy a
    # given result came from.
    source_type: str


def _looks_like_agenda_pdf(href: str, link_text: str) -> bool:
    if not is_navigable_href(href):
        return False

    path = href.split("?")[0]
    if not path.lower().endswith(".pdf"):
        return False

    # Only the filename, not the full path, feeds the keyword checks below —
    # AgendaCenter-style sites nest every document type under one generic
    # category folder (e.g. ".../agendas-and-minutes/city-council/..."), so
    # matching against the full href would flag every single document as a
    # "minutes" doc regardless of what it actually is. The filename itself
    # is where these platforms actually encode the specific document's body
    # and type (e.g. "20260714-city-council-special-and-regular-agenda.pdf").
    filename = path.rsplit("/", 1)[-1]

    # URL slugs hyphenate multi-word terms (e.g. "city-council-agenda.pdf")
    # where link text wouldn't — normalizing hyphens to spaces lets the
    # phrase keywords below match either form.
    haystack = f"{filename} {link_text}".lower().replace("-", " ")

    if any(keyword in haystack for keyword in EXCLUDED_KEYWORDS):
        return False

    return any(keyword in haystack for keyword in RELEVANT_BODY_KEYWORDS) and any(
        keyword in haystack for keyword in AGENDA_KEYWORDS
    )


def _extract_agenda_links(
    listing_url: str, html: str, jurisdiction: str, source_type: str
) -> list[ScrapedDocument]:
    """Parses a listing page's HTML for agenda/packet/staff-report PDF
    links. Deliberately platform-agnostic — just looks for <a href> tags
    anywhere on the page. Used by DirectPdfStrategy. Returns every match
    regardless of the date window — that filter is applied separately, as
    a post-fetch step (see ScraperStrategy._filter_by_date_window), not
    here."""
    soup = BeautifulSoup(html, "html.parser")
    documents: list[ScrapedDocument] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        text = anchor.get_text(strip=True)
        if not _looks_like_agenda_pdf(href, text):
            continue

        absolute_url = normalize_url(listing_url, href)
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        meeting_date = parse_meeting_date(f"{href} {text}")
        documents.append(
            ScrapedDocument(
                jurisdiction=jurisdiction,
                pdf_url=absolute_url,
                meeting_date=meeting_date,
                title=text or href,
                source_type=source_type,
            )
        )

    return documents


# ---------------------------------------------------------------------------
# Strategy Pattern: one concrete ScraperStrategy per agenda-portal vendor
# platform. See the module docstring for implementation status.
# ---------------------------------------------------------------------------
class ScraperStrategy(abc.ABC):
    """Common interface every portal-vendor strategy implements. A strategy
    must not let an expected request-level failure (bad status code, DNS/
    connection error) propagate — those should be caught and logged
    internally, returning [] instead, so one source's outage never breaks
    a sweep across every other configured source. get_strategy() below maps
    a MunicipalSource.type string to the matching strategy instance."""

    @abc.abstractmethod
    async def fetch_documents(
        self,
        source: MunicipalSource,
        client: AsyncSession,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[ScrapedDocument]:
        raise NotImplementedError

    def _resolve_date_window(
        self, start_date: datetime | None, end_date: datetime | None
    ) -> tuple[datetime, datetime]:
        """Fills in a sensible default window when either bound is
        omitted: end_date defaults to now, start_date defaults to
        DEFAULT_LOOKBACK_DAYS before end_date — so a daily delta cron job
        (or any other caller) that doesn't pass explicit bounds still gets
        a bounded, recent-only sweep rather than accidentally pulling a
        source's entire history."""
        resolved_end = end_date or datetime.now()
        resolved_start = start_date or (resolved_end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
        return resolved_start, resolved_end

    def _filter_by_date_window(
        self, documents: list[ScrapedDocument], start_date: datetime, end_date: datetime
    ) -> list[ScrapedDocument]:
        """Post-fetch date-window safety net shared by every strategy:
        drops any document whose meeting_date falls outside
        [start_date, end_date]. A document with no parsed meeting_date, or
        one whose meeting_date can't be parsed back into a real date, is
        kept rather than risk a false-negative exclusion — logged as a
        warning so that gap stays visible. Most strategies also filter
        earlier during extraction (see each subclass) for efficiency —
        this is what still catches anything that slipped through."""
        kept: list[ScrapedDocument] = []
        for document in documents:
            if document.meeting_date is None:
                logger.warning(
                    "[%s] %s has no parsed meeting_date — keeping it despite the date-window "
                    "filter rather than risk a false-negative exclusion.",
                    document.jurisdiction,
                    document.pdf_url,
                )
                kept.append(document)
                continue

            try:
                meeting_datetime = datetime.fromisoformat(document.meeting_date)
            except ValueError:
                logger.warning(
                    "[%s] %s has an unparseable meeting_date %r — keeping it despite the "
                    "date-window filter rather than risk a false-negative exclusion.",
                    document.jurisdiction,
                    document.pdf_url,
                    document.meeting_date,
                )
                kept.append(document)
                continue

            if start_date <= meeting_datetime <= end_date:
                kept.append(document)

        return kept


class DirectPdfStrategy(ScraperStrategy):
    """Handles a listing page with plain <a href="...pdf"> links — the
    only strategy with real scraping logic implemented so far. This is the
    same GET-and-parse logic this module used before the Strategy Pattern
    refactor, now returning the shared ScrapedDocument contract and using
    the centralized scraper_utils helpers."""

    async def fetch_documents(
        self,
        source: MunicipalSource,
        client: AsyncSession,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[ScrapedDocument]:
        start_date, end_date = self._resolve_date_window(start_date, end_date)
        try:
            response = await client.get(source.listing_url)
            response.raise_for_status()
        except HTTPError as exc:
            logger.error(
                "[%s] direct_pdf: HTTP %s fetching %s",
                source.name,
                exc.response.status_code,
                source.listing_url,
            )
            return []
        except RequestException as exc:
            logger.error(
                "[%s] direct_pdf: %s (%s) fetching %s",
                source.name,
                type(exc).__name__,
                exc,
                source.listing_url,
            )
            return []

        documents = _extract_agenda_links(
            source.listing_url, response.text, source.city, source.type
        )
        return self._filter_by_date_window(documents, start_date, end_date)


def _unwrap_google_docs_viewer_url(url: str) -> str:
    """Granicus sometimes links to a Google Docs Viewer wrapper
    (docs.google.com/gview?url=<encoded target>&embedded=true) instead of
    the real document directly — confirmed live for East Palo Alto: fetching
    the wrapper URL as-is returns an HTML viewer shell, not the PDF bytes
    themselves, which breaks ingestion. Extracts and unquotes the real
    target from the wrapper's `url` query parameter; returns `url`
    unchanged if it isn't a Google Docs Viewer link at all."""
    if "docs.google.com/gview" not in url.lower():
        return url

    query_params = parse_qs(urlparse(url).query)
    target = query_params.get("url")
    if not target:
        return url

    return unquote(target[0])


class GranicusStrategy(ScraperStrategy):
    """Handles Granicus/Legistar-style two-step portals: a calendar/table
    listing page whose rows link either straight to a PDF, or to an
    intermediate agenda-viewer page (AgendaViewer.php, AgendaOnline,
    ViewMeeting, or Legistar's Calendar.aspx flow) that itself links to the
    real PDF — one extra GET per row that isn't already a direct link."""

    # Recognized intermediate agenda-viewer page shapes that need a second
    # GET before the real PDF link is reachable. Anything else is treated
    # as unresolvable rather than guessed at.
    _INTERMEDIATE_PAGE_MARKERS = (
        "AgendaViewer.php",
        "AgendaOnline",
        "ViewMeeting",
        "Calendar.aspx",
    )
    # Substrings that identify the real PDF link on an intermediate page —
    # not always a plain ".pdf" extension (Legistar often serves it through
    # a query-string document-type/download endpoint instead).
    _PDF_LINK_MARKERS = (".pdf", "documenttype=5", "downloadfile")

    # Matches S3 virtual-hosted-style URLs (https://{bucket}.s3.amazonaws.com/{key}).
    # Only buckets whose name contains an underscore are actually affected
    # (see _fix_s3_ssl_url) — wildcard TLS certs don't cover underscores in
    # the leftmost DNS label, confirmed live as a certificate
    # hostname-mismatch error for Granicus's own attachment bucket.
    _S3_VIRTUAL_HOSTED_PATTERN = re.compile(r"^https://([^/]+)\.s3\.amazonaws\.com/(.+)$")

    async def fetch_documents(
        self,
        source: MunicipalSource,
        client: AsyncSession,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[ScrapedDocument]:
        start_date, end_date = self._resolve_date_window(start_date, end_date)
        try:
            response = await client.get(source.listing_url)
            response.raise_for_status()
        except HTTPError as exc:
            logger.error(
                "[%s] granicus_dom: HTTP %s fetching %s",
                source.name,
                exc.response.status_code,
                source.listing_url,
            )
            return []
        except RequestException as exc:
            logger.error(
                "[%s] granicus_dom: %s (%s) fetching %s",
                source.name,
                type(exc).__name__,
                exc,
                source.listing_url,
            )
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        documents: list[ScrapedDocument] = []

        # Granicus rows are often tagged with a class like "catAgendaRow",
        # but that varies by portal skin — just walking every <tr> and
        # looking for an "Agenda" link inside it is the safer, more
        # platform-agnostic approach (same philosophy as
        # _looks_like_agenda_pdf for the direct_pdf strategy).
        for row_index, row in enumerate(soup.find_all("tr")):
            try:
                agenda_link = next(
                    (
                        anchor
                        for anchor in row.find_all("a", href=True)
                        if is_navigable_href(anchor["href"])
                        and "agenda" in anchor.get_text(strip=True).lower()
                    ),
                    None,
                )
                if agenda_link is None:
                    continue

                # Granicus listing pages dump a jurisdiction's entire
                # history in one table — checking the row's own date
                # column *before* ever calling _resolve_pdf_url (which can
                # cost a whole extra network round-trip per row) means a
                # daily delta run skips the network cost for every
                # out-of-window historical row entirely, not just the
                # final result.
                row_text = row.get_text(" ", strip=True)
                meeting_date = parse_meeting_date(row_text)
                if meeting_date is None:
                    meeting_date = self._parse_date_from_headers_cell(row)
                if meeting_date is not None:
                    meeting_datetime = datetime.fromisoformat(meeting_date)
                    if not (start_date <= meeting_datetime <= end_date):
                        continue

                normalized_href = normalize_url(source.listing_url, agenda_link["href"])

                pdf_url = await self._resolve_pdf_url(normalized_href, client)
                if pdf_url is None:
                    logger.warning(
                        "[%s] granicus_dom: could not resolve a PDF link from %s — "
                        "skipping row.",
                        source.name,
                        normalized_href,
                    )
                    continue

                documents.append(
                    ScrapedDocument(
                        jurisdiction=source.city,
                        pdf_url=pdf_url,
                        meeting_date=meeting_date,
                        title=agenda_link.get_text(strip=True) or None,
                        source_type=source.type,
                    )
                )
            except Exception as exc:
                # Covers both malformed-row parsing and a failed secondary
                # request in _resolve_pdf_url — either way, one bad row
                # must never take down the rest of this jurisdiction's
                # sweep, let alone the whole multi-source run. Logs the row
                # index plus a text fragment (not the full row, which can
                # be arbitrarily large/noisy) so a real failure can be
                # traced back to the specific row that caused it instead of
                # guessing from the exception alone.
                row_fragment = row.get_text(" ", strip=True)[:120]
                logger.warning(
                    "[%s] granicus_dom: skipping row %d due to error: %s (row text: %r)",
                    source.name,
                    row_index,
                    exc,
                    row_fragment,
                )
                continue

        return self._filter_by_date_window(documents, start_date, end_date)

    # Bounds the recursion in _resolve_pdf_url below — confirmed live for
    # Redwood City that one redirect hop can land on yet another
    # intermediate page (AgendaViewer.php -> redirect ->
    # AgendaOnline/ViewMeeting), not the final file. A generous-but-finite
    # cap rather than unbounded recursion, in case a pathological portal
    # ever redirects in a cycle.
    _MAX_RESOLVE_DEPTH = 4

    async def _resolve_pdf_url(
        self, normalized_url: str, client: AsyncSession, _depth: int = 0
    ) -> str | None:
        """The two-step PDF resolution Granicus/Legistar portals need: if
        normalized_url already points straight at a PDF, that's the final
        URL. If it's a recognized intermediate agenda-viewer page, fetch
        that page — but some AgendaViewer.php links turn out to 302-redirect
        straight to the PDF, or serve it directly (Content-Type:
        application/pdf) rather than returning an HTML wrapper page at all,
        so both of those are intercepted *before* ever handing the response
        to BeautifulSoup, which would otherwise choke trying to parse
        redirect bodies/binary PDF bytes as HTML. Only once neither of
        those applies do we treat the response as HTML and look for the
        real PDF link inside it. Returns None for an unrecognized link
        shape, a redirect with no Location header, or no PDF link found on
        the intermediate page, rather than guess at a URL.

        A redirect's destination isn't always the final file either —
        confirmed live for Redwood City: AgendaViewer.php redirects to an
        AgendaOnline/ViewMeeting page, which is itself just another HTML
        wrapper whose real Documents/Downloadfile PDF link only turns up
        via the same anchor-scan below. So the redirect branch recurses
        back into this same function (bounded by _MAX_RESOLVE_DEPTH)
        instead of trusting the Location header as terminal.

        Deliberately doesn't catch its own request errors — the caller
        wraps each row (including this call) in one try/except, so a
        failed secondary request is handled the same way as any other
        row-level parsing failure."""
        if _depth > self._MAX_RESOLVE_DEPTH:
            return None
        # Handles the case where the row's own link is already a Google
        # Docs Viewer wrapper — unwrapping here means we can skip the
        # secondary fetch (and its HTML-shell response) entirely, since
        # the real target is embedded right in this URL's query string.
        normalized_url = _unwrap_google_docs_viewer_url(normalized_url)

        lowered_url = normalized_url.lower()

        if "documents/downloadfile" in lowered_url:
            # AgendaOnline's Documents/Downloadfile/{name}?documentType=...
            # &meetingId=...&isAttachment=True URLs (confirmed live:
            # Redwood City) look like a direct file link — the path even
            # ends in ".pdf" — but a plain GET only returns a client-side
            # "Downloading, please wait..." interstitial page. See
            # _resolve_downloadfile_url for the real two-step resolution.
            return await self._resolve_downloadfile_url(normalized_url, client)

        if (
            lowered_url.endswith(".pdf")
            or "view.ashx" in lowered_url
            or "showpublisheddocument" in lowered_url
        ):
            # Legistar's View.ashx?M=A&ID=...&GUID=... and Redwood City's
            # /ShowPublishedDocument/{id}/{token} both serve the PDF
            # directly (confirmed live: Content-Type: application/pdf)
            # despite having no .pdf suffix — no HTML page to navigate, so
            # no secondary GET is needed or wanted here.
            return self._fix_s3_ssl_url(normalized_url)

        if not any(marker in normalized_url for marker in self._INTERMEDIATE_PAGE_MARKERS):
            return None

        secondary_response = await client.get(normalized_url, allow_redirects=False)

        if secondary_response.status_code in (301, 302, 303, 307, 308):
            location = secondary_response.headers.get("Location")
            if not location:
                return None
            # East Palo Alto's AgendaViewer.php confirmed live: this
            # redirect can itself point straight at a Google Docs Viewer
            # wrapper rather than the real document — unwrap before
            # normalizing/returning it.
            location = _unwrap_google_docs_viewer_url(location)
            resolved_location = normalize_url(normalized_url, location)

            lowered_location = resolved_location.lower()
            lands_on_recognized_shape = (
                lowered_location.endswith(".pdf")
                or "view.ashx" in lowered_location
                or "showpublisheddocument" in lowered_location
                or any(
                    marker in resolved_location for marker in self._INTERMEDIATE_PAGE_MARKERS
                )
            )
            if lands_on_recognized_shape:
                # Redwood City confirmed live: this redirect can itself
                # land on just another intermediate wrapper page
                # (AgendaOnline/ViewMeeting) rather than the final file —
                # recurse so the same direct-serve/intermediate-page/
                # anchor-scan logic above runs again on it, instead of
                # trusting this Location header as automatically terminal.
                return await self._resolve_pdf_url(resolved_location, client, _depth + 1)

            # An unrecognized destination shape — matches this function's
            # original behavior of trusting a redirect's destination
            # directly (confirmed live for East Palo Alto's redirect
            # straight to a real S3-hosted PDF).
            return self._fix_s3_ssl_url(resolved_location)

        secondary_response.raise_for_status()

        content_type = secondary_response.headers.get("Content-Type", "").lower()
        if secondary_response.status_code == 200 and content_type.startswith("application/pdf"):
            return self._fix_s3_ssl_url(str(secondary_response.url))

        secondary_soup = BeautifulSoup(secondary_response.text, "html.parser")

        for anchor in secondary_soup.find_all("a", href=True):
            # Handles the case where the *intermediate page's* document
            # link is the Google Docs Viewer wrapper rather than the row's
            # own link (East Palo Alto's actual failure mode, confirmed
            # live) — unwrapped before the marker check below, since the
            # wrapper URL itself already contains ".pdf" in its encoded
            # query string and would otherwise match _PDF_LINK_MARKERS as
            # if it were a real, directly-downloadable PDF link.
            if not is_navigable_href(anchor["href"]):
                continue
            candidate = _unwrap_google_docs_viewer_url(anchor["href"])
            if any(marker in candidate.lower() for marker in self._PDF_LINK_MARKERS):
                # Recurses rather than trusting this match as automatically
                # terminal — a matched candidate can itself be another
                # AgendaOnline Documents/Downloadfile interstitial
                # (confirmed live: Redwood City's "Agenda Packet" anchor on
                # its ViewMeeting page), which the Documents/Downloadfile
                # check at the top of this function knows how to resolve
                # further; a plain .pdf/view.ashx match still resolves in
                # one extra, no-network call via that same check.
                return await self._resolve_pdf_url(
                    normalize_url(normalized_url, candidate), client, _depth + 1
                )

        return None

    async def _resolve_downloadfile_url(self, url: str, client: AsyncSession) -> str | None:
        """AgendaOnline's Documents/Downloadfile URL doesn't serve the file
        itself — it serves a "Downloading, please wait..." page whose
        embedded JS does the real work in two steps (confirmed live by
        reading that page's own script against Redwood City):
        1. POST Documents/InvokeDownloadAttachment/{name}?meetingId=...
           &itemId=0&publishId=0&isSection=false&documentType={type},
           which returns a small JSON object describing the resolved
           document (notably a DocumentType *string*, e.g. "AgendaPacket"
           — not the numeric type this step takes as input).
        2. GET Documents/ViewDocument/{name}?meetingId=...&documentType=
           {that string}&itemId=...&publishId=...&isSection=..., built
           from step 1's response, which serves the real PDF bytes.
        itemId/publishId/isSection are hardcoded to 0/0/false here,
        matching the page's own JS defaults for the isAttachment=True
        case this module's scraped links always carry; a link needing
        different values isn't supported and falls through to None below.
        Returns None if the URL is missing a required query field, or
        either network step fails, rather than guess at a URL that won't
        actually serve a PDF."""
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        document_type = next(iter(query.get("documentType", [])), None)
        meeting_id = next(iter(query.get("meetingId", [])), None)
        if document_type is None or meeting_id is None:
            return None

        document_name = unquote(parsed.path.rsplit("/", 1)[-1])
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        invoke_url = (
            f"{base_url}/AgendaOnline/Documents/InvokeDownloadAttachment/"
            f"{quote(document_name)}?meetingId={meeting_id}&itemId=0&publishId=0"
            f"&isSection=false&documentType={document_type}"
        )

        try:
            invoke_response = await client.post(invoke_url)
            invoke_response.raise_for_status()
            payload = invoke_response.json()
        except (RequestException, ValueError):
            return None

        resolved_name = payload.get("DocumentName")
        resolved_type = payload.get("DocumentType")
        resolved_meeting_id = payload.get("MeetingId")
        if not resolved_name or not resolved_type or resolved_meeting_id is None:
            return None

        is_section = "true" if payload.get("IsSection") else "false"
        return (
            f"{base_url}/AgendaOnline/Documents/ViewDocument/{quote(resolved_name)}"
            f"?meetingId={resolved_meeting_id}&documentType={resolved_type}"
            f"&itemId={payload.get('ItemId', 0)}&publishId={payload.get('PublishId', 0)}"
            f"&isSection={is_section}"
        )

    def _parse_date_from_headers_cell(self, row) -> str | None:
        """Fallback for Granicus skins (confirmed live: Burlingame) that
        render the meeting date in a dedicated <td headers="Date"> cell as
        a strict "MM/DD/YY" string (e.g. "07/06/26") instead of embedding a
        4-digit-year date anywhere in the row's own text — parse_meeting_date's
        patterns never match that 2-digit-year shape, so older archive rows
        (Burlingame's more recent rows also carry a "YYYY-MM-DD" phrase
        elsewhere in the row and never reach this path) were silently
        dropped. Only ever consulted by the caller when the generic
        row-text scan already came back None, so this can't override or
        change behavior for any other Granicus city's already-working
        parsing."""
        date_cell = row.find("td", attrs={"headers": "Date"})
        if date_cell is None:
            return None
        return parse_short_year_date(date_cell.get_text(strip=True))

    def _fix_s3_ssl_url(self, url: str) -> str:
        """S3 buckets whose name contains an underscore (e.g. Granicus's
        own "granicus_production_attachments") can't validate their TLS
        certificate when addressed in virtual-hosted style
        (https://{bucket}.s3.amazonaws.com/{key}) — wildcard certs don't
        cover underscores in the leftmost DNS label, confirmed live as a
        certificate hostname-mismatch ConnectError. Rewrites those specific
        URLs to S3 path-style addressing (https://s3.amazonaws.com/
        {bucket}/{key}) instead, which isn't subject to that restriction.
        Returns `url` unchanged for anything that doesn't match this exact
        problematic shape (including non-S3 URLs and S3 buckets without
        underscores, which work fine as-is)."""
        match = self._S3_VIRTUAL_HOSTED_PATTERN.match(url)
        if not match:
            return url

        bucket, object_key = match.groups()
        if "_" not in bucket:
            return url

        return f"https://s3.amazonaws.com/{bucket}/{object_key}"


class CivicPlusStrategy(ScraperStrategy):
    """Handles CivicPlus AgendaCenter portals: a listing page whose rows
    (commonly tagged with a "catAgendaRow" class, on either a <tr> or a
    <div>-based skin, or a "MeetingRow" class on IQM2/Legistar-Insight-style
    portals — confirmed live: Pacifica's actual agenda source is an IQM2
    Calendar.aspx page, not its CivicPlus-CMS-hosted "city-council-agendas"
    page) each show a meeting date and a handful of document links (agenda,
    minutes, addendum, ...). Some AgendaCenter portals render this list via
    client-side JS/AJAX rather than static HTML — those simply yield zero
    rows here (logged as 0 documents found) rather than fail outright,
    matching this module's fail-soft philosophy; confirm a given portal
    renders server-side before relying on it returning anything."""

    # Tried in order; the first class present wins over the generic
    # fallback sweep. "catAgendaRow" is CivicPlus's own AgendaCenter class;
    # "MeetingRow" is IQM2/Legistar-Insight's equivalent (confirmed live on
    # Pacifica's real Calendar.aspx page: <div class="Row MeetingRow">).
    _ROW_CLASSES = ("catAgendaRow", "MeetingRow")
    # Tried next, before the generic (fallback-prone) div/tr sweep: some
    # town-website CMSes (confirmed live: Portola Valley) list meetings in
    # a plain <table class="...listtable..."> with no repeating row class
    # at all, but where each direct <tr> child is already precisely
    # scoped to one meeting — unlike the generic fallback sweep below,
    # which walks every <tr>/<div> on the page and can match giant,
    # page-wide wrapper elements that happen to also contain a link and a
    # date somewhere in their (much larger) subtree.
    _TABLE_ROW_CLASS = "listtable"
    # A link only counts as a document link if its href contains one of
    # these (case-insensitive) — CivicPlus commonly serves the actual file
    # through a ViewFile-style download endpoint, not a plain .pdf suffix.
    # "showpublisheddocument" covers another CivicPlus-CMS document-viewer
    # pattern (confirmed live: Redwood City). "fileopen.aspx" covers
    # IQM2/Legistar-Insight's viewer (confirmed live: Pacifica's real
    # "FileOpen.aspx?Type=14&ID=...&Inline=True" agenda links, which carry
    # no "agenda"/".pdf" substring in the href itself — only in the link's
    # visible text, which _select_agenda_link's label-based preference
    # below already handles).
    _LINK_HREF_MARKERS = ("viewfile", "agenda", ".pdf", "showpublisheddocument", "fileopen.aspx")
    # Rejected outright regardless of any _LINK_HREF_MARKERS match — CMS
    # utility pages (email subscription signup, RSS-style notification
    # opt-in) rather than documents. Confirmed live: Pacifica's
    # "/i-want-to/request/city-council-agenda-notification" signup page
    # otherwise passes the bare "agenda" marker above.
    _REJECTED_HREF_WORDS = ("notification", "subscribe", "signup")
    # A candidate link is deprioritized (used only if nothing better is
    # found in the same row) if its text/aria-label suggests it's a
    # minutes/addendum/amendment document rather than the main agenda.
    _DEPRIORITIZED_LINK_WORDS = ("minutes", "addendum", "amendment")
    # Rejected outright (never selected, even as a last resort) when found
    # in an anchor's label OR href — this specific format variant is
    # guaranteed non-PDF. Confirmed live: Millbrae's AgendaCenter rows
    # offer separate HTML/PDF/Packet format-variant links side by side for
    # the same meeting, and the HTML one is labeled "HTML" with a literal
    # "?html=true" in its own href (shared with the row's main title link).
    _REJECTED_FORMAT_WORDS = ("html",)
    # Preferred, in order, once the "agenda"-labeled preference below
    # doesn't match anything — checked against label OR href. Rows that
    # offer separate format-variant links (confirmed live: Millbrae) often
    # don't label the real PDF link "agenda" at all, so this catches those
    # before falling through to an arbitrary "first remaining" pick, which
    # could just as easily land on the wrong format.
    _PREFERRED_FORMAT_WORDS = ("pdf", "packet")

    async def fetch_documents(
        self,
        source: MunicipalSource,
        client: AsyncSession,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[ScrapedDocument]:
        start_date, end_date = self._resolve_date_window(start_date, end_date)
        try:
            response = await client.get(source.listing_url)
            response.raise_for_status()
        except HTTPError as exc:
            logger.error(
                "[%s] civicplus_dom: HTTP %s fetching %s",
                source.name,
                exc.response.status_code,
                source.listing_url,
            )
            return []
        except RequestException as exc:
            logger.error(
                "[%s] civicplus_dom: %s (%s) fetching %s",
                source.name,
                type(exc).__name__,
                exc,
                source.listing_url,
            )
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        rows: list = []
        for row_class in self._ROW_CLASSES:
            rows = soup.find_all(class_=row_class)
            if rows:
                break
        if not rows:
            rows = self._find_table_rows(soup)
        if not rows:
            rows = self._find_fallback_rows(soup)

        documents: list[ScrapedDocument] = []
        seen_urls: set[str] = set()

        for row_index, row in enumerate(rows):
            try:
                link = self._select_agenda_link(row)
                if link is None:
                    continue

                pdf_url = normalize_url(source.listing_url, link["href"])
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                # A matched marker (e.g. "agenda", "viewfile") doesn't
                # guarantee the target is actually a PDF — only a literal
                # .pdf suffix does. Everything else gets a lightweight HEAD
                # (never a full GET, which would download the whole body
                # just to inspect a header) to confirm the real
                # Content-Type before it's ever queued for extraction.
                if not pdf_url.lower().endswith(".pdf"):
                    if not await self._verify_pdf_content_type(pdf_url, client, source.name):
                        continue

                meeting_date = parse_meeting_date(row.get_text(" ", strip=True))

                documents.append(
                    ScrapedDocument(
                        jurisdiction=source.city,
                        pdf_url=pdf_url,
                        meeting_date=meeting_date,
                        title=link.get_text(strip=True) or None,
                        source_type=source.type,
                    )
                )
            except Exception as exc:
                # A logged HTML snippet (not the whole row, which can be
                # arbitrarily large for a fallback-matched wrapper div)
                # lets a real failure be traced back to the specific
                # markup that caused it, without guessing from the
                # exception message alone. One bad row must never take
                # down the rest of this jurisdiction's sweep.
                row_fragment = str(row)[:200]
                logger.warning(
                    "[%s] civicplus_dom: skipping row %d due to error: %s (html: %r)",
                    source.name,
                    row_index,
                    exc,
                    row_fragment,
                )
                continue

        return self._filter_by_date_window(documents, start_date, end_date)

    def _find_fallback_rows(self, soup: BeautifulSoup) -> list:
        """When no element carries the catAgendaRow class, falls back to
        any <tr> or <div> containing both a link and a parseable meeting
        date — a platform-agnostic sweep for AgendaCenter skins that use a
        different wrapper. This is prone to matching the same entry at
        multiple nesting levels (a row's parent div also "contains" its
        link and date) — fetch_documents' seen_urls dedup absorbs that,
        rather than this needing precise DOM-shape detection."""
        candidates = []
        for element in soup.find_all(["tr", "div"]):
            if not element.find("a", href=True):
                continue
            if parse_meeting_date(element.get_text(" ", strip=True)) is None:
                continue
            candidates.append(element)
        return candidates

    def _find_table_rows(self, soup: BeautifulSoup) -> list:
        """Looks for a <table class="...listtable..."> (confirmed live:
        Portola Valley) and, if found, returns its direct <tr> children
        as-is — each one is already precisely scoped to a single meeting
        (or, for a header row, matches no document link at all and is
        simply skipped downstream), so there's no need for the generic
        fallback sweep's link+date heuristic here. Returns [] if no such
        table exists on the page, letting the caller fall through to that
        generic sweep instead."""
        table = soup.find("table", class_=self._TABLE_ROW_CLASS)
        if table is None:
            return []
        return table.find_all("tr")

    def _select_agenda_link(self, row):
        """Picks the best document link within one row. Only hrefs
        matching _LINK_HREF_MARKERS count as document links at all, and a
        candidate whose label or href flags it as the "HTML" format
        variant is excluded outright (see _REJECTED_FORMAT_WORDS) — never
        selected, even as a last resort. Among what's left, prefers one
        whose visible text or aria-label says "agenda" and isn't
        deprioritized (minutes/addendum/amendment); failing that, prefers
        one labeled "PDF" or "Packet" (see _PREFERRED_FORMAT_WORDS —
        AgendaCenter rows that offer separate format-variant links often
        don't label the real PDF link "agenda" at all); falling back from
        there to the first non-deprioritized document link, and finally to
        whatever document link is present if every candidate looks like a
        minutes/addendum document."""

        def format_haystack(anchor) -> str:
            return f"{anchor.get_text(strip=True)} {anchor.get('aria-label', '')} {anchor['href']}".lower()

        candidates = [
            anchor
            for anchor in row.find_all("a", href=True)
            if is_navigable_href(anchor["href"])
            and not any(word in anchor["href"].lower() for word in self._REJECTED_HREF_WORDS)
            and any(marker in anchor["href"].lower() for marker in self._LINK_HREF_MARKERS)
            and not any(word in format_haystack(anchor) for word in self._REJECTED_FORMAT_WORDS)
        ]
        if not candidates:
            return None

        def label(anchor) -> str:
            return f"{anchor.get_text(strip=True)} {anchor.get('aria-label', '')}".lower()

        def is_deprioritized(anchor) -> bool:
            return any(word in label(anchor) for word in self._DEPRIORITIZED_LINK_WORDS)

        agenda_labeled = [a for a in candidates if "agenda" in label(a) and not is_deprioritized(a)]
        if agenda_labeled:
            return agenda_labeled[0]

        pdf_or_packet_labeled = [
            a
            for a in candidates
            if any(word in format_haystack(a) for word in self._PREFERRED_FORMAT_WORDS)
            and not is_deprioritized(a)
        ]
        if pdf_or_packet_labeled:
            return pdf_or_packet_labeled[0]

        non_deprioritized = [a for a in candidates if not is_deprioritized(a)]
        if non_deprioritized:
            return non_deprioritized[0]

        return candidates[0]

    async def _verify_pdf_content_type(
        self, url: str, client: AsyncSession, source_name: str
    ) -> bool:
        """Confirms `url` actually serves a PDF via a HEAD request — no
        response body downloaded — rather than trusting the href-marker
        match alone. Returns False (the row should be dropped) for a
        non-"application/pdf" Content-Type, a missing one, or a failed
        request, since queuing a false positive (an HTML page like
        Pacifica's notification/signup pages, confirmed live) into the
        Gemini extraction pipeline wastes API credits on a document that
        was never real.

        Some portals' document-viewer endpoints don't support HEAD at all
        — confirmed live for Portola Valley's (and Redwood City's)
        ShowPublishedDocument links: HEAD returns 404 even though GET
        serves the real PDF. A bare 404/405 on HEAD (not a raised
        exception — the endpoint responded, just not to this method) is
        treated as "HEAD unsupported" and retried via a streamed GET,
        which reads only the response headers before immediately closing
        the connection — still no response body downloaded, just a
        different request method to get there."""
        try:
            response = await client.head(url)
        except RequestException as exc:
            logger.warning(
                "[%s] civicplus_dom: HEAD request failed verifying %s (%s) — dropping.",
                source_name,
                url,
                exc,
            )
            return False

        if response.status_code in (404, 405):
            try:
                response = await client.get(url, stream=True)
            except RequestException as exc:
                logger.warning(
                    "[%s] civicplus_dom: HEAD unsupported (%s) and streamed GET failed "
                    "verifying %s (%s) — dropping.",
                    source_name,
                    response.status_code,
                    url,
                    exc,
                )
                return False
            try:
                content_type = response.headers.get("Content-Type", "").lower()
            finally:
                await response.aclose()
        else:
            content_type = response.headers.get("Content-Type", "").lower()

        if not content_type.startswith("application/pdf"):
            logger.info(
                "[%s] civicplus_dom: %s is not a PDF (Content-Type: %s) — dropping.",
                source_name,
                url,
                content_type or "<missing>",
            )
            return False
        return True


def _first_present(item: dict, keys: tuple[str, ...]):
    """Returns item[key] for the first of `keys` that's actually present,
    or raises KeyError(keys) if none of them are — used so a vendor's JSON
    schema is matched via a fallback chain of known field-name aliases,
    while still raising a normal KeyError (caught by the per-item handling
    in ApiStrategy.fetch_documents) when the vendor has renamed/removed all
    of them."""
    for key in keys:
        if key in item:
            return item[key]
    raise KeyError(keys)


def _civicclerk_date(item: dict):
    return _first_present(item, ("startDateTime", "eventDate", "meetingDate", "date"))


def _civicclerk_base_api_url(listing_url: str) -> str:
    """Derives a CivicClerk tenant's base API URL (e.g.
    "https://brisbaneca.api.civicclerk.com/v1") from its /v1/Events listing
    URL, by dropping the trailing entity-set segment — the file-stream
    endpoint lives under the same base, just a different OData function
    call ("Meetings/GetMeetingFileStream(...)" instead of "Events")."""
    return listing_url.rstrip("/").rsplit("/", 1)[0]


# Real CivicClerk /v1/Events items (confirmed live against a real tenant)
# nest their document list under `publishedFiles`, not a generically-named
# "files"/"documents" array as originally assumed — each entry has a
# `fileId` (its own `id` field is always 0) and a `type`/`name` describing
# what it is (e.g. "Agenda", "Agenda Packet", "Minutes"). Other tenants may
# use a differently-cased/named key, so several are tried in order.
_CIVICCLERK_FILE_LIST_KEYS = (
    "publishedFiles",
    "Files",
    "MeetingFiles",
    "Documents",
    "files",
    "documents",
)
# "Minutes" documents record what already happened, not the live agenda —
# skipped even when no better candidate exists in the same file list.
_CIVICCLERK_EXCLUDED_FILE_WORDS = ("minutes",)


def _civicclerk_file_stream_url(item: dict, base_api_url: str) -> str:
    """CivicClerk's /v1/Events items don't carry a direct document URL at
    all — the real PDF is served through a separate OData function-call
    endpoint, {base_api_url}/Meetings/GetMeetingFileStream(fileId=...,
    plainText=false), keyed by the fileId of an entry in the event's
    nested file list (see _CIVICCLERK_FILE_LIST_KEYS). Raises KeyError if
    no usable (non-"minutes") file entry with a fileId is found in any of
    the known list keys."""
    for list_key in _CIVICCLERK_FILE_LIST_KEYS:
        file_list = item.get(list_key)
        if not file_list:
            continue

        candidates = [
            entry
            for entry in file_list
            if (entry.get("fileId") or entry.get("id"))
            and not any(
                word in str(entry.get("type") or entry.get("name") or "").lower()
                for word in _CIVICCLERK_EXCLUDED_FILE_WORDS
            )
        ]
        if not candidates:
            continue

        def preference_rank(entry: dict) -> int:
            label = str(entry.get("type") or entry.get("name") or "").lower()
            return 0 if "agenda" in label else 1

        chosen = min(candidates, key=preference_rank)
        file_id = chosen.get("fileId") or chosen.get("id")
        return f"{base_api_url}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"

    raise KeyError(_CIVICCLERK_FILE_LIST_KEYS)


def _primegov_date(item: dict):
    """PrimeGov's ListArchivedMeetings items carry both `dateTime` (ISO
    8601, e.g. "2026-07-13T18:00:00") and `date` (a human-readable string,
    e.g. "Jul 13, 2026") on the same object simultaneously — confirmed
    against a real tenant. Prefers dateTime as the more precise,
    unambiguous machine format; falls back to `date` only when dateTime is
    missing or empty, rather than picking whichever of the two happens to
    exist first. The caller (ApiStrategy.fetch_documents) still runs the
    result through parse_meeting_date to normalize either shape into
    "YYYY-MM-DD"."""
    return item.get("dateTime") or item.get("date")


# compileOutputType: 1 = PDF, 3 = HTML (confirmed against real PrimeGov
# ListArchivedMeetings responses) — the same templateName (e.g. "Agenda")
# can appear twice in one meeting's documentList, once per output type, so
# only a type-1 entry is ever eligible for the PDF-parsing ingestion
# pipeline; the type-3 sibling 302-redirects to an HTML rendering instead.
_PRIMEGOV_PDF_COMPILE_OUTPUT_TYPE = 1

# A documentList entry only counts as an agenda document if its
# templateName contains one of these (case-insensitive) — listed in
# preference order, so "agenda" wins over "packet" when a meeting has both.
_PRIMEGOV_TEMPLATE_KEYWORDS = ("agenda", "packet")

# /Portal/viewer?id={id}&type=2 (this module's previous choice) only
# serves PrimeGov's Accusoft HTML viewer wrapper — confirmed live it
# returns Content-Type: text/html, never the PDF itself. This path
# instead 302-redirects straight to a real, time-limited signed Azure
# Blob Storage URL serving the actual PDF (confirmed live against both
# San Mateo and Atherton: Content-Type: application/pdf, real %PDF-1.7
# bytes) — no viewer session, no extra request needed, since curl_cffi's
# AsyncSession already follows redirects (allow_redirects=True). Keyed by
# the documentList entry's own `id`, a root-relative path resolved to the
# tenant's domain (e.g. "https://sanmateo.primegov.com") via normalize_url
# at the ApiStrategy call site below, the same established pattern used
# for every other relative URL this module produces.
_PRIMEGOV_COMPILED_DOCUMENT_PATH_TEMPLATE = "/Public/CompiledDocument/{document_id}"


def _primegov_pdf_url(item: dict) -> str | None:
    """Picks the best PDF-compiled (compileOutputType == 1) agenda/packet
    document from one meeting's documentList and returns the viewer path
    PrimeGov serves it at, or None if documentList is empty/absent or
    nothing in it qualifies — a meeting can legitimately have no PDF
    agenda posted yet, which is an expected "skip this meeting" outcome
    for the caller (see `if not pdf_url: continue`), not a warning-worthy
    unexpected shape."""
    document_list = item.get("documentList")
    if not isinstance(document_list, list) or not document_list:
        return None

    candidates = [
        doc
        for doc in document_list
        if doc.get("id") is not None
        and doc.get("compileOutputType") == _PRIMEGOV_PDF_COMPILE_OUTPUT_TYPE
        and any(
            keyword in str(doc.get("templateName", "")).lower()
            for keyword in _PRIMEGOV_TEMPLATE_KEYWORDS
        )
    ]
    if not candidates:
        return None

    def preference_rank(doc: dict) -> int:
        name = str(doc.get("templateName", "")).lower()
        for rank, keyword in enumerate(_PRIMEGOV_TEMPLATE_KEYWORDS):
            if keyword in name:
                return rank
        return len(_PRIMEGOV_TEMPLATE_KEYWORDS)

    chosen = min(candidates, key=preference_rank)
    return _PRIMEGOV_COMPILED_DOCUMENT_PATH_TEMPLATE.format(document_id=chosen["id"])


def _primegov_base_url(listing_url: str) -> str:
    """Strips any query string from a configured PrimeGov listing URL
    (e.g. a leftover hardcoded "?year=2026" in config/sources.py) so
    fetch_documents can safely append its own dynamically-computed
    "?year={year}" for each year spanned by the requested date window,
    regardless of what's baked into the config entry."""
    parts = urlsplit(listing_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _years_spanning(start_date: datetime, end_date: datetime) -> list[int]:
    """Every calendar year touched by [start_date, end_date] — PrimeGov's
    ListArchivedMeetings endpoint is scoped to a single year per request,
    so a window straddling a year boundary (e.g. Dec 2025-Jan 2026) needs
    a separate fetch for each year involved."""
    return list(range(start_date.year, end_date.year + 1))


async def _fetch_json_array(
    client: AsyncSession, url: str, source_name: str
) -> list | None:
    """Fetches `url`, parses it as JSON, and normalizes it down to a bare
    list — unwrapping a "value"/"data" OData-style wrapper if present
    (confirmed live for CivicClerk's /v1/Events endpoint). Returns None
    (having already logged why) on any failure — a bad HTTP status,
    connection error, non-JSON body, or a JSON shape that isn't a
    recognized array/wrapper — so callers can just skip this fetch rather
    than duplicating error handling. Shared by both ApiStrategy's
    single-fetch (CivicClerk) and per-year multi-fetch (PrimeGov) paths."""
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except HTTPError as exc:
        logger.error(
            "[%s] api: HTTP %s fetching %s", source_name, exc.response.status_code, url
        )
        return None
    except RequestException as exc:
        logger.error(
            "[%s] api: %s (%s) fetching %s", source_name, type(exc).__name__, exc, url
        )
        return None
    except ValueError as exc:
        # response.json() failed to parse — e.g. the endpoint returned
        # HTML (a login/error page) instead of the expected JSON body.
        logger.error(
            "[%s] api: response was not valid JSON fetching %s (%s)", source_name, url, exc
        )
        return None

    if isinstance(payload, dict):
        if "value" in payload:
            payload = payload["value"]
        elif "data" in payload:
            payload = payload["data"]
        else:
            logger.warning(
                "[%s] api: got a JSON object with no known wrapper key (top-level keys: "
                "%s) — skipping %s.",
                source_name,
                list(payload.keys()),
                url,
            )
            return None

    if not isinstance(payload, list):
        logger.warning(
            "[%s] api: expected a JSON array, got %s fetching %s — skipping.",
            source_name,
            type(payload).__name__,
            url,
        )
        return None

    return payload


class ApiStrategy(ScraperStrategy):
    """Handles jurisdictions exposing a JSON meeting-list API (CivicClerk,
    PrimeGov) instead of an HTML listing page. Vendor is identified from
    the URL itself (see _VENDOR_EXTRACTORS below); each vendor's field
    names are tried via a fallback chain of known aliases, and any item
    whose shape doesn't match any of them is skipped (logged, not raised)
    so a vendor schema change can't take down the whole sweep."""

    async def fetch_documents(
        self,
        source: MunicipalSource,
        client: AsyncSession,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[ScrapedDocument]:
        start_date, end_date = self._resolve_date_window(start_date, end_date)

        listing_url_lower = source.listing_url.lower()
        is_civicclerk = "civicclerk" in listing_url_lower
        is_primegov = "primegov" in listing_url_lower
        if not is_civicclerk and not is_primegov:
            logger.warning(
                "[%s] api: unrecognized API vendor in %s — no field mapping known, skipping.",
                source.name,
                source.listing_url,
            )
            return []

        items: list[dict] = []
        if is_primegov:
            # PrimeGov's ListArchivedMeetings endpoint is scoped to a
            # single year per request — loop over every year the
            # requested window touches (e.g. a window spanning Dec
            # 2025-Jan 2026 needs both ?year=2025 and ?year=2026) instead
            # of relying on a single hardcoded year baked into
            # config/sources.py.
            base_url = _primegov_base_url(source.listing_url)
            for year in _years_spanning(start_date, end_date):
                year_items = await _fetch_json_array(
                    client, f"{base_url}?year={year}", source.name
                )
                if year_items:
                    items.extend(year_items)
            if not items:
                return []
        else:
            fetched = await _fetch_json_array(client, source.listing_url, source.name)
            if fetched is None:
                return []
            items = fetched

        if is_civicclerk:
            extract_date = _civicclerk_date
            base_api_url = _civicclerk_base_api_url(source.listing_url)
        else:
            extract_date = _primegov_date
            base_api_url = None

        documents: list[ScrapedDocument] = []
        for item in items:
            try:
                raw_date = extract_date(item)
            except (KeyError, TypeError) as exc:
                logger.warning(
                    "[%s] api: skipping item with unexpected JSON shape (%s)",
                    source.name,
                    exc,
                )
                continue

            # Dropped before ever attempting to construct a document URL
            # (a separate network round-trip for CivicClerk, or extra
            # work either way) — both to enforce the exact
            # [start_date, end_date] window and to avoid wasted effort on
            # events we don't want anyway.
            meeting_date = parse_meeting_date(str(raw_date)) if raw_date else None
            if meeting_date is not None:
                meeting_datetime = datetime.fromisoformat(meeting_date)
                if not (start_date <= meeting_datetime <= end_date):
                    continue

            try:
                # CivicClerk needs its own URL construction (a separate
                # OData function-call endpoint, not a field on the event
                # itself) — PrimeGov keeps using its normal extraction,
                # unchanged.
                if is_civicclerk:
                    pdf_url = _civicclerk_file_stream_url(item, base_api_url)
                else:
                    pdf_url = _primegov_pdf_url(item)
            except (KeyError, TypeError) as exc:
                logger.warning(
                    "[%s] api: skipping item with unexpected JSON shape (%s)",
                    source.name,
                    exc,
                )
                continue

            if not pdf_url:
                continue

            documents.append(
                ScrapedDocument(
                    jurisdiction=source.city,
                    pdf_url=normalize_url(source.listing_url, pdf_url),
                    meeting_date=meeting_date,
                    title=None,
                    source_type=source.type,
                )
            )

        return self._filter_by_date_window(documents, start_date, end_date)


class _UnrecognizedTypeStrategy(ScraperStrategy):
    """Fallback for a MunicipalSource.type with no registered strategy at
    all (e.g. a config typo) — logs clearly and no-ops rather than raising,
    so a bad config entry can't crash the whole sweep."""

    def __init__(self, source_type: str) -> None:
        self._source_type = source_type

    async def fetch_documents(
        self,
        source: MunicipalSource,
        client: AsyncSession,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[ScrapedDocument]:
        logger.error(
            "[%s] has unrecognized source type '%s' — no strategy registered. Skipping.",
            source.name,
            self._source_type,
        )
        return []


_STRATEGIES_BY_TYPE: dict[str, type[ScraperStrategy]] = {
    "direct_pdf": DirectPdfStrategy,
    "granicus_dom": GranicusStrategy,
    "civicplus_dom": CivicPlusStrategy,
    "api": ApiStrategy,
}


def get_strategy(source_type: str) -> ScraperStrategy:
    """Factory: maps a MunicipalSource.type string to its ScraperStrategy
    instance. An unrecognized type gets the same treatment as an
    unimplemented one — logged clearly and given a no-op strategy — rather
    than raising and aborting the whole sweep."""
    strategy_class = _STRATEGIES_BY_TYPE.get(source_type)
    if strategy_class is None:
        return _UnrecognizedTypeStrategy(source_type)
    return strategy_class()


async def scrape_source(
    source: MunicipalSource,
    client: AsyncSession,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[ScrapedDocument]:
    """Orchestrates one source: looks up its ScraperStrategy via
    source.type and runs it. Each strategy already catches its own expected
    request-level failures (see DirectPdfStrategy) and logs a clear,
    specific error; this is a second, outer safety net catching anything
    unexpected (e.g. a bug inside a strategy) so a single source can never
    take down a sweep across every other configured source in
    find_new_documents."""
    try:
        return await get_strategy(source.type).fetch_documents(source, client, start_date, end_date)
    except Exception:
        logger.exception(
            "[%s] Unexpected error while scraping (type=%s, url=%s) — skipping this source.",
            source.name,
            source.type,
            source.listing_url,
        )
        return []


async def find_new_documents(
    sources: list[MunicipalSource] | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[ScrapedDocument]:
    """Scrapes all (or the given) sources — optionally restricted to
    documents dated within [start_date, end_date] (each strategy defaults
    missing bounds to a recent, bounded window — see
    ScraperStrategy._resolve_date_window — so a daily delta cron job never
    accidentally pulls a source's entire history) — and filters out any
    PDF URL already downloaded in a previous run, per local_store's
    scraped-URL dedup set — never re-downloads or re-processes the same
    URL twice.

    Each source is fully error-isolated (see scrape_source): a connection
    failure, bad HTTP status, or unexpected exception for one jurisdiction
    is logged and skipped, never aborting the loop for the remaining
    sources."""
    sources = SOURCES if sources is None else sources
    new_documents: list[ScrapedDocument] = []

    async with AsyncSession(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=get_default_headers(),
        impersonate="chrome",
        allow_redirects=True,
    ) as client:
        for source in sources:
            for document in await scrape_source(source, client, start_date, end_date):
                if not local_store.is_scraped_url(document.pdf_url):
                    new_documents.append(document)

    return new_documents


async def download_document(url: str, client: AsyncSession) -> str:
    """Streams the PDF to a temp file on disk instead of buffering the
    whole response in memory — some agenda packets run 40+ MB (confirmed
    live against a real Menlo Park Planning Commission packet), and
    holding that fully in RAM for every concurrent ingest job doesn't
    scale. Returns the temp file's path; the caller owns deleting it once
    done (see routers/ingest.py's _run_ingest_job, the single place every
    caller — the scraper router, cron_scrape.py, and the manual upload
    endpoint — routes through)."""
    response = await client.get(url, stream=True)
    response.raise_for_status()

    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in response.aiter_content():
                f.write(chunk)
    except Exception:
        os.unlink(path)
        raise
    return path
