# Seats.aero award availability reporter

Queries the Seats.aero Partner API (cached-data tier, Pro key) for four
reports:

1. **TPE arrivals** from major US gateways, Dec 16-21 2026, all cabins,
   business/first flagged separately.
2. **Cheapest award routes** departing RIC or IAD in the next 30 days -
   the 25 cheapest distinct *routes* (by origin/destination airport pair).
   Each route appears once with its different mileage-program/cabin price
   points nested underneath as "sub routes", instead of repeating the same
   route once per price point.
3. **TPE back to North America**, Dec 29 2026 - Jan 3 2027 (the return leg),
   same all-cabins / business-first breakdown as report 1.
4. **RIC <-> SMF self-connect deals**: since seats.aero likely has no
   direct cached data for that specific small-airport pair, this searches
   RIC and SMF each against a list of major US hubs, matches up same-day
   legs, then looks up each leg's *real* flight times (a separate API
   endpoint) to confirm a genuine 1-stop connection with a 45min-3h
   layover exists. Covers the next 3 months.

**Duplicate routes are collapsed.** A 30-day or 3-month search naturally
finds the same route at the same price on many different dates. Instead of
20 nearly-identical rows, each report groups by route+program+cabin+price
and shows one row with every matching date listed in a "Dates" column.

## Two ways to use this

- **`app.py`** - a small Flask web app: a URL you open in a browser, with a
  per-report "Refresh now" button and a CSV download link. This is the one
  to deploy so you have a bookmarkable dashboard instead of a terminal.
- **`seats_aero_report.py`** - the original command-line version: prints
  tables to the terminal and writes CSVs to `output/`.

Both are thin presentation layers over **`seats_aero_core.py`**, which has
all the actual API-calling/parsing logic in one place, so the two front
ends can never drift out of sync with each other.

## Install (either way)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Configure your API key

Copy the example env file and fill in your Pro API key (from your
seats.aero account):

```bash
cp .env.example .env
# then edit .env and paste your key after SEATS_AERO_API_KEY=
```

Both `app.py` and `seats_aero_report.py` read `SEATS_AERO_API_KEY` from
that `.env` file (or from a real environment variable, which is how you'll
set it once this is deployed - see below). The key is never hardcoded or
printed.

## Running the web dashboard locally

```bash
python app.py
```

Then open **http://localhost:5000** - you'll see links to all 4 reports,
each with its own Refresh button. First load of a report does a real API
call; after that it's instant until you click Refresh.

## Deploying it as a real website (Render, free tier)

This puts the dashboard at a public `https://something.onrender.com` URL
you can open from your phone or bookmark, with your API key stored as a
server-side secret - never sent to or through anyone else, including me.

1. **Push this repo to GitHub** (already done, if you're reading this from
   the repo).
2. Go to **https://render.com**, sign up/log in (GitHub login is easiest).
3. **New +** -> **Web Service** -> connect your GitHub account -> pick this
   repo.
4. Fill in:
   - **Root Directory**: leave blank (this repo's files are already at the
     repo root)
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app` (Render also auto-detects this
     from the included `Procfile`)
   - **Instance Type**: Free
5. Under **Environment**, add these environment variables:
   - `SEATS_AERO_API_KEY` = your real Pro key
   - `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` = pick any
     username/password (see **Security** below - strongly recommended once
     this is public)
6. **Create Web Service**. First deploy takes a couple minutes; Render
   gives you the public URL once it's live.

**Free-tier quirk:** Render's free web services spin down after ~15
minutes of no traffic and take ~30-50 seconds to wake back up on the next
request. That's normal - the first load after being idle just looks slow,
it's not broken.

### Security

This page can trigger real Seats.aero API calls (spending your daily
quota) and shows your personal travel search results. Once it's on a
public URL, anyone who finds it could hammer your Refresh buttons or just
browse your searches. Setting `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`
(step 5 above) puts the whole site behind a login prompt (HTTP Basic
Auth) - do this before sharing the URL with anyone, including yourself on
another device you haven't set up yet. Leaving them unset is fine only for
`python app.py` on your own machine.

### Keeping the deployed key safe

Never commit your real key to `.env` and push it - `.env` is already in
`.gitignore` for exactly this reason. On Render, the key lives only in
that service's **Environment** settings, encrypted at rest, and is
injected into the running process - it's never in your repo, never in
this chat, and never visible in the page's HTML.

## Running the CLI version

```bash
python seats_aero_report.py
```

Add `--refresh` to bypass the local cache and re-query the API even if a
cached response already exists:

```bash
python seats_aero_report.py --refresh
```

Each run prints how many Seats.aero API calls you have left today (from
the response's rate-limit header), so you can keep an eye on your daily
Pro-tier quota.

## Notes

- Uses only the **Cached Search** endpoint (`GET /partnerapi/search`),
  which searches multiple origins against multiple destinations across
  *all* mileage programs in one call. This is a deliberate choice over the
  **Bulk Availability** endpoint (`GET /partnerapi/availability`), which
  only covers one mileage program per call and doesn't accept a
  specific-airport filter - a worse fit (and far more API calls) for
  "search these few origin airports across every program."
- Live Search (real-time pricing) is never used - it's a commercial-only
  feature and Pro keys can't call it.
- Report 2 asks for "any destination," but Cached Search requires an
  explicit destination list (there's no wildcard). The script searches
  against a curated list of ~70 major airports worldwide as a practical
  stand-in for "anywhere" - see `REPORT2_DESTINATIONS` in
  `seats_aero_core.py` if you want to edit that list.
- Report 4 also uses the **Get Trips** endpoint (`GET
  /partnerapi/trips/{id}`) to fetch real flight departure/arrival times for
  each candidate leg - Cached Search's summary rows only say a cabin is
  available on a date, not what time. This costs one extra API call per
  leg checked, capped at `SELF_CONNECT_MAX_LOOKUPS` (default 60) cheapest
  candidates so quota use stays predictable; increase it in
  `seats_aero_core.py` if you want a deeper search. On the web dashboard,
  Report 4's Refresh can take up to a minute because of these extra calls
  - that's expected, not a hang.
- Report 4's connections are **two separately-ticketed award bookings**,
  not one protected itinerary - if the first flight runs late, the second
  isn't held or refunded for you. A `SELF_CONNECT_MIN_LAYOVER_MIN` (default
  45 minutes) is enforced in addition to your 3-hour max, since a shorter
  self-transfer generally isn't realistically bookable. Both constants are
  easy to change in `seats_aero_core.py`.
- The API-response cache in `cache/` is just local files, so on a host
  with ephemeral disk (like Render's free tier) it resets on every
  redeploy/restart - normal, and only means the next load after a restart
  does one real fetch instead of a cache hit.
