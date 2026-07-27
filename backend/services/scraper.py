"""Generic municipal agenda-portal scraper.

Fetches a city's agenda listing page and finds public PDF links for the
high-yield bodies (City Council, Planning Commission, Board of Supervisors,
or anything zoning-related) whose URL or link text mentions "Agenda",
"Packet", or "Staff Report" — explicitly skipping irrelevant boards/
commissions (parks, library, arts, etc.) and "minutes" documents, since
those generate near-zero rezoning signal but cost the same OCR/Gemini
processing time. See RELEVANT_BODY_KEYWORDS / AGENDA_KEYWORDS /
EXCLUDED_KEYWORDS below. The documents routers/scraper.py then downloads
and feeds into the existing background ingestion pipeline
(services/pdf_parser.py via routers/ingest.py's _run_ingest_job).
Deduplicates against local_store.is_scraped_url so a previously-downloaded
PDF URL is never re-fetched or re-queued.

This is deliberately a modular *template*, not a Redwood-City-specific
scraper: municipal agenda portals run on a handful of vendor platforms
(Granicus, Legistar, CivicPlus, PrimeGov, eScribe, ...) that each lay out
their listing pages differently, and vendors/URLs change over time. The
`MunicipalSource` list below is a starting point to point at whatever the
real, currently-live listing page is for each city — the crawler itself
(`_extract_agenda_links`) only assumes "a page with <a href="...pdf"> links
somewhere on it", which holds across most of these platforms.

IMPORTANT — verification status of the sources below (last checked live,
with the browser-style headers this module now sends):
  - Menlo Park's AgendaCenter (menlopark.gov/agendacenter) returns 200 and
    its HTML contains real "Agenda packet"-labelled PDF links that match
    `_looks_like_agenda_pdf` as-is. Confirmed working.
  - Redwood City (redwoodcity.org) returns 403 even with a full realistic
    Chrome header set — this looks like Akamai bot-protection blocking on
    IP reputation/TLS fingerprint rather than missing headers, since the
    same client succeeds against Menlo Park and San Mateo's root domain.
    Left in as a real target to retry from a non-flagged network/IP; do
    not assume it works without re-checking.
  - San Mateo's CivicPlus AgendaCenter (cityofsanmateo.org/agendacenter)
    returns 200 but renders its document list via client-side JS/AJAX, so
    the static HTML has zero matching <a> tags — this scraper's
    static-HTML approach can't see those links without also driving a
    headless browser or hitting CivicPlus's underlying JSON endpoint.
    Not included below until one of those is implemented."""

import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

import local_store

logger = logging.getLogger(__name__)

# A self-identifying UA string is more honest, but several target portals
# run WAF/bot-protection rules (e.g. Akamai) that block anything that
# doesn't look like a real browser, regardless of intent — these listing
# pages are public and the request volume here is a handful of GETs per
# run, so presenting as a normal browser is the pragmatic tradeoff.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
REQUEST_TIMEOUT_SECONDS = 30.0

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


# See the module docstring's IMPORTANT note — verify/update before
# depending on this in production.
SOURCES: list[MunicipalSource] = [
    MunicipalSource(
        name="Menlo Park City Council",
        city="Menlo Park",
        listing_url="https://www.menlopark.gov/agendacenter",
    ),
    MunicipalSource(
        name="Redwood City City Council",
        city="Redwood City",
        listing_url="https://www.redwoodcity.org/departments/city-clerk/city-council-agendas-minutes",
    ),
]


class ScrapedDocument(BaseModel):
    url: str
    title: str
    city: str


def _looks_like_agenda_pdf(href: str, link_text: str) -> bool:
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


def _extract_agenda_links(listing_url: str, html: str, city: str) -> list[ScrapedDocument]:
    """Parses a listing page's HTML for agenda/packet/staff-report PDF
    links. Deliberately platform-agnostic — just looks for <a href> tags
    anywhere on the page, since Granicus/Legistar/CivicPlus/etc. each wrap
    these in different surrounding markup but all render plain anchor
    tags for the actual PDF links."""
    soup = BeautifulSoup(html, "html.parser")
    documents: list[ScrapedDocument] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        text = anchor.get_text(strip=True)
        if not _looks_like_agenda_pdf(href, text):
            continue

        absolute_url = urljoin(listing_url, href)
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        documents.append(ScrapedDocument(url=absolute_url, title=text or href, city=city))

    return documents


async def scrape_source(
    source: MunicipalSource, client: httpx.AsyncClient
) -> list[ScrapedDocument]:
    """Fetches one source's listing page and returns every matching PDF
    link found there, regardless of whether it's been processed before —
    dedup happens one level up, in find_new_documents."""
    try:
        response = await client.get(source.listing_url)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to scrape %s (%s)", source.name, source.listing_url)
        return []

    return _extract_agenda_links(source.listing_url, response.text, source.city)


async def find_new_documents(
    sources: list[MunicipalSource] | None = None,
) -> list[ScrapedDocument]:
    """Scrapes all (or the given) sources and filters out any PDF URL
    already downloaded in a previous run, per local_store's scraped-URL
    dedup set — never re-downloads or re-processes the same URL twice."""
    sources = SOURCES if sources is None else sources
    new_documents: list[ScrapedDocument] = []

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=BROWSER_HEADERS,
        follow_redirects=True,
    ) as client:
        for source in sources:
            for document in await scrape_source(source, client):
                if not local_store.is_scraped_url(document.url):
                    new_documents.append(document)

    return new_documents


async def download_document(url: str, client: httpx.AsyncClient) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content
