"""Raw scraper source configuration.

One entry per San Mateo County jurisdiction to poll, plus which portal-
scraping strategy applies to it. This is deliberately plain data (no
Pydantic here) — it's the config layer, not the runtime model — consumed
by services/scraper.py, which adapts each entry into the runtime
MunicipalSource list and dispatches on `type` to the matching
ScraperStrategy (see get_strategy() there).

`type` values, one bucket per municipal agenda-portal vendor platform:
  - "direct_pdf": a listing page with plain <a href="...pdf"> links.
    The only strategy with real scraping logic implemented so far.
  - "granicus_dom": a Granicus/Legistar-style calendar page whose entries
    link to a per-meeting page, which then links to the actual PDF — a
    two-step navigation the direct_pdf strategy can't follow. Not yet
    implemented (see services/scraper.py's GranicusStrategy).
  - "civicplus_dom": a CivicPlus AgendaCenter portal. Some render a plain
    document list server-side (compatible with direct_pdf); others render
    it via client-side JS/AJAX, which a static HTML GET can't see. Not yet
    implemented (see CivicPlusStrategy) — pending a per-portal check of
    which behavior applies before reusing direct_pdf here.
  - "api": the jurisdiction exposes a JSON meeting-list API (CivicClerk,
    PrimeGov, etc.) instead of an HTML listing page. Not yet implemented
    (see ApiStrategy).

IMPORTANT: none of these listing-page URLs have been fetched/verified live
against the real sites while writing this config — confirm each still
resolves to a real, current agenda listing page before relying on it, the
same as the module docstring in services/scraper.py already warns for the
sources checked so far.
"""

from typing import TypedDict


class SourceConfig(TypedDict):
    jurisdiction: str
    url: str
    type: str


SOURCES: list[SourceConfig] = [
    # --- Direct PDF Bucket -------------------------------------------------
    {
        "jurisdiction": "Menlo Park",
        "url": "https://www.menlopark.gov/Agendas-and-minutes",
        "type": "direct_pdf",
    },
    # --- Granicus / Legistar Bucket -----------------------------------------
    {
        "jurisdiction": "Redwood City",
        # The city's own page (still the human-facing source, kept in a
        # comment for reference) just embeds this real Granicus listing
        # via an iframe/JS widget — our static-HTML scraper never followed
        # that embed, so it found effectively nothing. Pointing directly
        # at the real Granicus ViewPublisher.php page (confirmed live: 961
        # real agenda rows), the same pattern already working for
        # Burlingame's "burlingameca.granicus.com/ViewPublisher.php".
        # Old URL: https://www.redwoodcity.org/departments/city-clerk/
        # city-council-meetings-agendas-and-minutes
        "url": "https://redwoodcity-ca.granicus.com/ViewPublisher.php?view_id=2",
        "type": "granicus_dom",
    },
    {
        "jurisdiction": "Belmont",
        "url": "https://www.belmont.gov/departments/meetings-agendas-minutes",
        "type": "granicus_dom",
    },
    {
        # Burlingame's city.gov agendas page is just a wrapper around this
        # Granicus portal — live testing against it as civicplus_dom found
        # zero matching rows, since it isn't a CivicPlus AgendaCenter at
        # all. This is the real underlying listing page.
        "jurisdiction": "Burlingame",
        "url": "https://burlingameca.granicus.com/ViewPublisher.php?view_id=3",
        "type": "granicus_dom",
    },
    {
        "jurisdiction": "South San Francisco",
        "url": "https://ci-ssf-ca.legistar.com/Calendar.aspx",
        "type": "granicus_dom",
    },
    {
        # The eastpaloalto.primegov.com domain from an earlier revision
        # doesn't resolve at all (DNS failure) — this is the real portal,
        # same "ghost city" wrapper-site pattern as Burlingame/Half Moon Bay.
        "jurisdiction": "East Palo Alto",
        "url": "https://cityofepa.granicus.com/ViewPublisher.php?view_id=1",
        "type": "granicus_dom",
    },
    {
        # This Legistar calendar covers Board of Supervisors business for
        # the county's unincorporated areas, not the incorporated cities
        # (each of which has its own source entry above/below) — the more
        # specific name avoids implying county-wide overlap with them.
        "jurisdiction": "Unincorporated San Mateo County",
        "url": "https://sanmateocounty.legistar.com/Calendar.aspx",
        "type": "granicus_dom",
    },
    {
        "jurisdiction": "San Carlos",
        # Live testing found the public-facing city.gov page has zero <tr>
        # rows at all (its agenda list renders via an embedded widget, not
        # static HTML) — this is the actual underlying Granicus/iQM2 portal
        # that widget points at.
        "url": "https://sancarlosca.iqm2.com/Citizens/Default.aspx",
        "type": "granicus_dom",
    },
    {
        # Same story as Burlingame/San Carlos above: the city.gov agendas
        # page is a wrapper around this real Granicus portal.
        "jurisdiction": "Half Moon Bay",
        "url": "https://halfmoonbay.granicus.com/ViewPublisher.php?view_id=1",
        "type": "granicus_dom",
    },
    # --- CivicPlus AgendaCenter Bucket --------------------------------------
    {
        "jurisdiction": "San Bruno",
        "url": "https://sanbruno.ca.gov/AgendaCenter/City-Council-17",
        "type": "civicplus_dom",
    },
    {
        "jurisdiction": "Hillsborough",
        "url": "https://www.hillsborough.net/AgendaCenter/",
        "type": "civicplus_dom",
    },
    {
        "jurisdiction": "Millbrae",
        "url": "https://www.ci.millbrae.ca.us/AgendaCenter",
        "type": "civicplus_dom",
    },
    {
        "jurisdiction": "Pacifica",
        # Pacifica's real agenda source is this IQM2/Legistar-Insight
        # Calendar.aspx portal, not its CivicPlus-CMS "city-council-agendas"
        # page (confirmed live: that page is a flat document archive with
        # no real per-meeting agenda rows). Year bounds are static, like
        # PrimeGov's own hardcoded "?year=2026" config entries elsewhere in
        # this file — update annually.
        "url": "https://pacificacityca.iqm2.com/Citizens/Calendar.aspx?From=1/1/2026&To=12/31/2026",
        "type": "civicplus_dom",
    },
    {
        "jurisdiction": "Woodside",
        "url": "https://www.woodsideca.gov/agendacenter",
        "type": "civicplus_dom",
    },
    {
        "jurisdiction": "Portola Valley",
        "url": (
            "https://www.portolavalley.net/town-government/town-council/minutes-and-agendas"
        ),
        "type": "civicplus_dom",
    },
    # --- Modern API Bucket ---------------------------------------------------
    # type is "api" for all of these, regardless of vendor (CivicClerk vs.
    # PrimeGov) — services/scraper.py's ApiStrategy detects the specific
    # vendor from the URL itself (looks for "civicclerk"/"primegov")  and
    # applies that vendor's field-name mapping, so there's no separate
    # "civicclerk"/"primegov" `type` to register here.
    {
        "jurisdiction": "Brisbane",
        "url": "https://brisbaneca.api.civicclerk.com/v1/Events",
        "type": "api",
    },
    {
        "jurisdiction": "San Mateo",
        "url": "https://sanmateo.primegov.com/api/v2/PublicPortal/ListArchivedMeetings?year=2026",
        "type": "api",
    },
    {
        # The city.gov agendas page turned out to be a wrapper around this
        # real PrimeGov portal, same pattern as the Granicus "ghost city"
        # wrapper sites above.
        "jurisdiction": "Atherton",
        "url": "https://atherton.primegov.com/api/v2/PublicPortal/ListArchivedMeetings?year=2026",
        "type": "api",
    },
    {
        "jurisdiction": "Foster City",
        "url": "https://fostercity.primegov.com/api/v2/PublicPortal/ListArchivedMeetings?year=2026",
        "type": "api",
    },
    {
        "jurisdiction": "Colma",
        "url": "https://colmaca.api.civicclerk.com/v1/Events",
        "type": "api",
    },
    {
        "jurisdiction": "Daly City",
        "url": "https://dalycityca.api.civicclerk.com/v1/Events",
        "type": "api",
    },
]
