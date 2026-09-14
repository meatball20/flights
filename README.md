# Seats.aero award availability reporter

A small command-line script that queries the Seats.aero Partner API
(cached-data tier, Pro key) and prints four reports:

1. **TPE arrivals** from major US gateways, Dec 16-21 2026, all cabins,
   business/first flagged separately.
2. **Cheapest award routes** departing RIC or IAD in the next 30 days
   (top 25 distinct routes by miles).
3. **TPE back to North America**, Dec 29 2026 - Jan 3 2027 (the return leg),
   same all-cabins / business-first breakdown as report 1.
4. **RIC <-> SMF self-connect deals**: since seats.aero likely has no
   direct cached data for that specific small-airport pair, this searches
   RIC and SMF each against a list of major US hubs, matches up same-day
   legs, then looks up each leg's *real* flight times (a separate API
   endpoint) to confirm a genuine 1-stop connection with a 45min-3h
   layover exists. Covers the next 3 months.

All reports print as tables in the terminal and are written to CSV files in
`output/`. Raw API responses are cached as JSON in `cache/` so you can
re-run the analysis without spending API quota.

**Duplicate routes are collapsed.** A 30-day or 3-month search naturally
finds the same route at the same price on many different dates. Instead of
printing 20 nearly-identical rows, each report groups by
route+program+cabin+price and shows one row with every matching date listed
in a "Dates" column (truncated with a "+N more" note if there are a lot).

## Install

Clone this repo, then, from inside it:

**macOS / Linux (bash/zsh):**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
If `Activate.ps1` is blocked by your execution policy, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then retry. Or
skip activation and just call `venv\Scripts\python.exe` /
`venv\Scripts\pip.exe` directly in the commands below.

## Configure your API key

Copy the example env file and fill in your Pro API key (from your
seats.aero account):

**macOS / Linux:**
```bash
cp .env.example .env
```

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

Then open `.env` in any text editor and paste your key after
`SEATS_AERO_API_KEY=`.

The script reads `SEATS_AERO_API_KEY` from that `.env` file (or from your
shell environment if you'd rather `export` it directly). The key is never
hardcoded or printed.

## Run

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
  stand-in for "anywhere" - see `REPORT2_DESTINATIONS` in the script if
  you want to edit that list.
- Report 4 also uses the **Get Trips** endpoint (`GET
  /partnerapi/trips/{id}`) to fetch real flight departure/arrival times for
  each candidate leg - Cached Search's summary rows only say a cabin is
  available on a date, not what time. This costs one extra API call per
  leg checked, capped at `SELF_CONNECT_MAX_LOOKUPS` (default 60) cheapest
  candidates so quota use stays predictable; increase it in the script if
  you want a deeper search.
- Report 4's connections are **two separately-ticketed award bookings**,
  not one protected itinerary - if the first flight runs late, the second
  isn't held or refunded for you. A `SELF_CONNECT_MIN_LAYOVER_MIN` (default
  45 minutes) is enforced in addition to your 3-hour max, since a shorter
  self-transfer generally isn't realistically bookable. Both constants are
  easy to change at the top of the script.
