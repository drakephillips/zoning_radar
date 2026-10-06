# Zoning Radar

Turns San Mateo County city council agendas into a structured dataset of
land use decisions. It scrapes agenda PDFs from 21 cities, pulls out rezonings,
text amendments and entitlements, matches the affected parcels to assessor
APNs, and stores everything in Postgres with PostGIS.

This information is already public, but nobody can actually read it. It sits in
60 page meeting packets, one per city every couple of weeks, spread across four
different agenda portal vendors. This makes it queryable.

Right now it holds 202 agendas from 16 cities, 69 extracted land use items, and
80 resolved parcels.

## How it works

```
sources.py          21 cities, each using one of four strategies
   |                direct_pdf (1), granicus (8), civicplus (6), api (6)
   v
scraper.py          finds agenda, packet and staff report PDFs on each portal
   v
pdf_parser.py       PyMuPDF text extraction, OCR fallback for scanned pages
   |
   +-- Gemini, in two stages:
   |     1. classify the document using only the front matter, over a small
   |        input, then lock that result in
   |     2. extract items with the classification already decided
   |     They're split because CEQA boilerplate and General Plan references
   |     drown out the real project intent if both happen in one call.
   |
   +-- a regex tier that runs when Gemini isn't available
   v
parcel_resolver.py  APN resolution and address lookup
   v
Postgres + PostGIS  parcels, documents, rezoning_leads, lead_parcels
```

Every item stores the document and page number it came from, so any row can be
traced back to the paragraph that produced it.

## Data quality

The two extraction paths produce very different quality, which matters if you
use this data.

**Policy amendments (11 rows) are reliable.** These all came from the LLM path.
Each one has a real entitlement type, like Zoning Text Amendment, Local Coastal
Program Amendment or Initiative Ordinance Repeal, and the right scope.

**Site specific items (58 rows) are not validated.** They point at real parcels,
but they were matched on document structure instead of land use language, and
most have no entitlement type or meeting date. They're worth checking by hand.
They are not a dataset of decisions.

### The audit

In October 2026 I checked the stored data against the source documents and
found that 15% of the parcels were phone numbers. They were city switchboards
and Zoom dial-ins printed in agenda headers. San Mateo APNs look like
`###-###-###` and phone numbers look like `###-###-####`, and nothing was
checking the shape.

It wasn't one bug. It was four things stacking up:

1. The Gemini API started returning `402 RESOURCE_EXHAUSTED` once the credits
   ran out.
2. `extract_signals_from_pdf` caught that and quietly dropped the whole
   document down to the regex tier.
3. The last step of that tier, `_global_apn_sweep`, scans for anything shaped
   like an APN instead of returning nothing.
4. Those results skip `_has_required_fields`, because that check would throw
   out everything the fallback produces.

Each one is reasonable on its own. Together they turned a billing problem into
records that looked real. Those rows have been deleted.

**Still to do:** check the APN shape and reject `###-###-####`, put
`_global_apn_sweep` behind a flag, store an `extraction_method` on each row so
the two paths can be told apart, and mark a document FAILED when the extraction
API errors instead of silently falling back. That last one matters most. I'd
rather it stop than keep running and write junk.

## Stack

Python 3.13 and FastAPI, PyMuPDF, Google Gemini, Supabase (Postgres + PostGIS),
Next.js 15 with TypeScript and Tailwind.

## Running it

```bash
# backend
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY
.venv/bin/uvicorn main:app --reload --port 8000

# frontend
cd frontend && npm install && npm run dev
```

`database/schema.sql` and `database/migrations/` build the schema.
`backend/cron_scrape.py` is the scheduled entry point. It runs the same scrape,
extract and persist cycle as `POST /api/v1/scraper/run`.

Without `GEMINI_API_KEY` the pipeline still runs end to end on the regex tier.
Read the data quality section first for what that output is and isn't good for.
