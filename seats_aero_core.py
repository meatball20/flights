"""
Seats.aero Partner API client + report-building logic (Pro/cached-data tier).

This module has NO printing and writes NO files except the local API-response
cache in cache/ - it just fetches data and returns plain dicts/lists. Both
the terminal tool (seats_aero_report.py) and the web dashboard (app.py)
import this module and are just two different ways of *presenting* the same
data, so the fetching/parsing logic only exists in one place.

See seats_aero_report.py's old docstring (still at the top of that file) for
the full research notes on why Cached Search is used over Bulk Availability,
and why report 4 also needs the Get Trips endpoint.

Docs consulted (Pro/cached-tier endpoints only):
  https://developers.seats.aero/reference/cached-search
  https://developers.seats.aero/reference/get-availability   (Bulk Availability)
  https://developers.seats.aero/reference/get-trips
  https://developers.seats.aero/reference/getting-started-p  (auth header)
  https://docs.seats.aero/article/68-seatsaero-pro-api-access-limits-and-usage
"""

import csv
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Setup: paths, constants, and the .env file
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

# On a normal host (your machine, Render) the project folder is writable, so
# cache/ and output/ live right next to this script. Some hosts (e.g.
# Vercel's serverless functions) only allow writing to /tmp - if you ever
# deploy there, set VERCEL=1 (Vercel sets it automatically) to switch.
if os.environ.get("VERCEL"):
    CACHE_DIR = Path("/tmp/seats_aero_cache")
    OUTPUT_DIR = Path("/tmp/seats_aero_output")
else:
    CACHE_DIR = SCRIPT_DIR / "cache"
    OUTPUT_DIR = SCRIPT_DIR / "output"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# python-dotenv reads a .env file (if present) and loads its KEY=VALUE lines
# into the environment. Locally this lets SEATS_AERO_API_KEY live in a .env
# file; when deployed (e.g. on Render) there's no .env file and the key
# instead comes from a real environment variable set in the host's
# dashboard - load_dotenv() is a no-op if no .env file exists, so the same
# code works in both places.
load_dotenv(SCRIPT_DIR / ".env")

BASE_URL = "https://seats.aero/partnerapi"
SEARCH_URL = f"{BASE_URL}/search"
TRIPS_URL = f"{BASE_URL}/trips"

# Pagination: the API returns up to `take` rows per call and tells us if
# there's more via "hasMore" + a "cursor" to pass on the next call. We cap
# the number of pages we'll follow per query as a safety net against an
# unexpectedly huge result set eating your daily quota.
PAGE_SIZE = 1000
MAX_PAGES = 10

# Cabin codes the API uses internally, and how we want to label them.
CABIN_LABELS = {
    "Y": "Economy",
    "W": "Premium Economy",
    "J": "Business",
    "F": "First",
}
PREMIUM_CABINS = {"J", "F"}  # business/first, flagged separately per the ask

# ---------------------------------------------------------------------------
# Airport lists
# ---------------------------------------------------------------------------

REPORT1_ORIGINS = [
    "SFO", "LAX", "SEA", "SJC", "JFK", "EWR", "IAD", "IAH", "ORD", "DFW",
    "ONT", "ATL", "BOS", "MIA", "DEN", "LAS", "PHX", "HNL",
]
REPORT1_DESTINATION = "TPE"
REPORT1_START = "2026-12-16"
REPORT1_END = "2026-12-21"

REPORT2_ORIGINS = ["RIC", "IAD"]
REPORT2_DESTINATIONS = [
    # North America
    "JFK", "EWR", "LAX", "SFO", "ORD", "MIA", "ATL", "SEA", "DFW", "IAH",
    "BOS", "DEN", "LAS", "PHX", "YYZ", "YVR", "YUL", "MEX", "CUN", "HNL",
    # South America
    "GRU", "EZE", "BOG", "LIM", "SCL", "GIG",
    # Europe
    "LHR", "CDG", "FRA", "AMS", "MAD", "FCO", "MUC", "ZRH", "VIE", "CPH",
    "IST", "LIS", "BCN", "DUB", "ARN", "WAW", "BRU", "HEL", "ATH",
    # Middle East
    "DXB", "DOH", "AUH", "TLV", "RUH",
    # Africa
    "JNB", "CAI", "NBO", "CMN", "ADD",
    # Asia
    "NRT", "HND", "ICN", "PVG", "PEK", "HKG", "TPE", "SIN", "BKK", "KUL",
    "MNL", "DEL", "BOM", "CGK",
    # Oceania
    "SYD", "MEL", "BNE", "AKL", "NAN",
]
REPORT2_TOP_N = 25

REPORT3_ORIGIN = "TPE"
REPORT3_DESTINATIONS = REPORT1_ORIGINS + ["YYZ", "YVR", "YUL", "YYC", "MEX", "CUN"]
REPORT3_START = "2026-12-29"
REPORT3_END = "2027-01-03"

SELF_CONNECT_AIRPORTS = ("RIC", "SMF")
SELF_CONNECT_HUBS = [
    "ORD", "DFW", "ATL", "DEN", "IAH", "CLT", "PHX", "SLC", "MSP", "LAS",
    "SEA", "EWR", "JFK", "PHL", "DTW", "LAX", "SFO",
]
SELF_CONNECT_MAX_LAYOVER_MIN = 180  # "3 hours is the longest" rule
# Not requested, but added as a sanity floor: a same-day self-transfer under
# ~45 minutes generally isn't realistically bookable/safe (these are two
# *separate* award tickets, not one protected itinerary - no rebooking if
# leg 1 runs late). Change/remove if you'd rather see those too.
SELF_CONNECT_MIN_LAYOVER_MIN = 45
# How many of the cheapest same-day leg-pairs we'll spend extra API calls on
# to verify with real flight times (Get Trips costs one call per leg looked
# up). Keeps quota use predictable even if there turn out to be hundreds of
# candidate date/hub combinations. Overridable via an env var if you ever
# want to tune it without editing code (e.g. lower it on a host with a
# request time limit).
SELF_CONNECT_MAX_LOOKUPS = int(os.environ.get("SELF_CONNECT_MAX_LOOKUPS", "60"))


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------

def get_api_key():
    """Read the API key from the environment (or a .env file), or exit.

    Called once at process startup (CLI's main(), or app.py at import time)
    so a missing key fails loudly and immediately rather than mid-request.
    """
    api_key = os.environ.get("SEATS_AERO_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: SEATS_AERO_API_KEY is not set.\n"
            "Locally: export it, or create a .env file next to this script "
            "(copy .env.example to .env and fill in your key).\n"
            "On a host like Render: set it as an environment variable in "
            "the service's dashboard."
        )
    return api_key


def log_remaining_quota(response):
    """Print how many API calls are left today, from the response headers.

    Seats.aero returns your remaining daily quota in a rate-limit header.
    `requests` headers are case-insensitive, so this works regardless of
    exactly how the header name is capitalized on the wire. This prints to
    the console/server log either way - it's operational visibility, not
    something shown to a dashboard viewer.
    """
    remaining = response.headers.get("X-RateLimit-Remaining")
    limit = response.headers.get("X-RateLimit-Limit")
    if remaining is not None:
        print(f"  [quota] API calls remaining today: {remaining}"
              + (f" / {limit}" if limit else ""))
    else:
        rate_headers = {k: v for k, v in response.headers.items()
                         if "ratelimit" in k.lower() or "remaining" in k.lower()}
        if rate_headers:
            print(f"  [quota] rate-limit headers seen: {rate_headers}")


def cached_search(api_key, origin_airport, destination_airport, start_date,
                   end_date, cache_key, refresh=False):
    """Call Cached Search (GET /partnerapi/search) and return all rows.

    Handles pagination (cursor/hasMore) and caches the combined raw JSON
    response to a local file so re-running the analysis doesn't re-hit the
    API or spend quota. The cache file's mtime doubles as "when was this
    data last actually fetched" for display purposes - see
    latest_cache_timestamp() below.
    """
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists() and not refresh:
        print(f"[cache] Using cached response: {cache_file.name}")
        with open(cache_file) as f:
            return json.load(f)

    headers = {
        "Partner-Authorization": api_key,
        "Accept": "application/json",
    }
    all_rows = []
    cursor = None
    for page in range(1, MAX_PAGES + 1):
        params = {
            "origin_airport": origin_airport,
            "destination_airport": destination_airport,
            "start_date": start_date,
            "end_date": end_date,
            "take": PAGE_SIZE,
        }
        if cursor:
            params["cursor"] = cursor

        print(f"[api] GET /search page {page} "
              f"({origin_airport} -> {destination_airport}, "
              f"{start_date}..{end_date})")
        response = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        log_remaining_quota(response)

        if response.status_code != 200:
            raise RuntimeError(
                f"Seats.aero API returned {response.status_code} "
                f"for {response.url}: {response.text[:500]}"
            )

        payload = response.json()
        all_rows.extend(payload.get("data", []))

        if payload.get("hasMore"):
            # Trust "hasMore" alone (not cursor's truthiness) - a cursor of
            # 0 could be a legitimate pagination position, not "no cursor".
            cursor = payload.get("cursor")
            time.sleep(0.25)  # be polite between paginated calls
        else:
            break

    with open(cache_file, "w") as f:
        json.dump(all_rows, f)
    print(f"[cache] Saved {len(all_rows)} raw rows to {cache_file.name}")
    return all_rows


def get_trip_options(api_key, availability_id, refresh=False):
    """Call Get Trips (GET /partnerapi/trips/{id}) for one availability row
    and return its actual flight-level options: a list of dicts with
    departs_at / arrives_at (real datetimes), cabin, and stops.

    An availability row from Cached Search is a *summary* (e.g. "Economy
    available on this date for 70,000 miles") - it doesn't say what time
    the flight leaves. Get Trips looks up the real flight(s) behind one
    summary row. There can be more than one (e.g. two flight-time options
    on the same date/cabin/price), which is why this returns a list.

    Cached like cached_search(): one small JSON file per availability ID.
    """
    if not availability_id:
        return []

    cache_file = CACHE_DIR / f"trip_{availability_id}.json"
    if cache_file.exists() and not refresh:
        with open(cache_file) as f:
            payload = json.load(f)
    else:
        headers = {
            "Partner-Authorization": api_key,
            "Accept": "application/json",
        }
        url = f"{TRIPS_URL}/{availability_id}"
        response = requests.get(url, headers=headers, timeout=30)
        log_remaining_quota(response)
        if response.status_code != 200:
            print(f"  [warn] Get Trips failed ({response.status_code}) for "
                  f"{availability_id}; skipping this leg.")
            return []
        payload = response.json()
        with open(cache_file, "w") as f:
            json.dump(payload, f)

    options = []
    for trip in payload.get("data", []):
        departs = parse_iso_datetime(trip.get("DepartsAt"))
        arrives = parse_iso_datetime(trip.get("ArrivesAt"))
        if departs is None or arrives is None:
            continue
        options.append({
            "departs_at": departs,
            "arrives_at": arrives,
            "cabin": trip.get("Cabin", ""),
            "stops": trip.get("Stops", 0),
        })
    return options


def parse_iso_datetime(value):
    """Parse an ISO-8601 timestamp like '2026-12-16T08:15:00Z' safely
    across Python versions (older versions' datetime.fromisoformat doesn't
    accept a trailing 'Z')."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def latest_cache_timestamp(cache_keys):
    """Human-readable 'last actually fetched from the API' time for one or
    more cache_key values, taken from the underlying JSON file's mtime
    (which updates whenever cached_search() does a live fetch, and stays
    put on a cache hit). Used to show "as of ..." on the dashboard."""
    if isinstance(cache_keys, str):
        cache_keys = [cache_keys]
    mtimes = []
    for key in cache_keys:
        path = CACHE_DIR / f"{key}.json"
        if path.exists():
            mtimes.append(path.stat().st_mtime)
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Turning raw API rows into per-cabin records we can sort/filter/export
# ---------------------------------------------------------------------------

def format_money(cents, currency):
    """Seats.aero reports taxes/fees in the smallest currency unit (e.g.
    cents for USD), so divide by 100 for a normal-looking amount."""
    if cents is None:
        return ""
    return f"{cents / 100:,.2f} {currency}".strip()


def explode_rows(raw_rows):
    """Turn each raw availability row (which bundles all 4 cabins) into one
    record per cabin that actually has availability.

    Each raw row looks roughly like:
      {
        "ID": "abc123", "Date": "2026-12-16",
        "Route": {"OriginAirport": "SFO", "DestinationAirport": "TPE"},
        "Source": "united",
        "YAvailable": true, "YMileageCostRaw": 70000, "YRemainingSeats": 2,
        "YTotalTaxes": 5600, "TaxesCurrency": "USD", "YDirect": true,
        ... (same pattern repeated for W, J, F) ...
      }
    We flatten that into up to 4 separate records, one per available cabin.
    "id" is kept on each record because report 4 needs it to look up real
    flight times via Get Trips.
    """
    records = []
    for row in raw_rows:
        route = row.get("Route", {})
        origin = route.get("OriginAirport", "")
        destination = route.get("DestinationAirport", "")
        program = row.get("Source", "")
        travel_date = row.get("Date", "")
        currency = row.get("TaxesCurrency", "")

        for cabin_code, cabin_name in CABIN_LABELS.items():
            if not row.get(f"{cabin_code}Available"):
                continue
            miles = row.get(f"{cabin_code}MileageCostRaw")
            seats = row.get(f"{cabin_code}RemainingSeats")
            taxes_cents = row.get(f"{cabin_code}TotalTaxes")
            nonstop = row.get(f"{cabin_code}Direct")

            records.append({
                "id": row.get("ID", ""),
                "cabin_code": cabin_code,
                "program": program,
                "miles": miles,
                "taxes": format_money(taxes_cents, currency),
                "taxes_cents": taxes_cents or 0,
                "cabin": cabin_name,
                "premium_cabin": cabin_code in PREMIUM_CABINS,
                "seats": seats,
                "origin": origin,
                "destination": destination,
                "date": travel_date,
                "routing": "Nonstop" if nonstop else "Connecting",
            })
    return records


# ---------------------------------------------------------------------------
# De-duplicating repeated route/price rows across many dates
# ---------------------------------------------------------------------------

def format_dates_list(dates, max_shown=6):
    """'2026-12-16, 2026-12-17, ... (+9 more)' style summary of a date list."""
    dates = sorted(dates)
    if len(dates) <= max_shown:
        return ", ".join(dates)
    shown = ", ".join(dates[:max_shown])
    return f"{shown} (+{len(dates) - max_shown} more)"


def collapse_duplicate_routes(records, key_fields):
    """Merge records that are the *same route at the same price* on
    different dates into a single row with a combined "dates" field.

    Without this, a 30-day search naturally returns the same route/program/
    price repeated on many dates - e.g. "United SFO->TPE Economy 70,000
    miles" showing up as 15 nearly-identical rows, one per date. This groups
    those by `key_fields` (a tuple of record keys, e.g. program/cabin/
    origin/destination/miles/taxes) and keeps one row per unique
    combination, listing every date it's available under a "dates" field.
    Seats shown is the best (max) seen across the group, since that's the
    most you could actually book on your best date.
    """
    groups = {}
    for r in records:
        key = tuple(r[f] for f in key_fields)
        group = groups.setdefault(key, {**r, "_dates": set(), "_seats": []})
        group["_dates"].add(r["date"])
        if r.get("seats") is not None:
            group["_seats"].append(r["seats"])

    collapsed = []
    for group in groups.values():
        group["dates"] = format_dates_list(group["_dates"])
        group["date_count"] = len(group["_dates"])
        group["seats"] = max(group["_seats"]) if group["_seats"] else None
        del group["_dates"]
        del group["_seats"]
        collapsed.append(group)
    return collapsed


# ---------------------------------------------------------------------------
# Grouping price points under one row per physical route (origin+destination)
# ---------------------------------------------------------------------------

ROUTE_DEDUPE_KEY = ("program", "cabin", "origin", "destination", "miles", "taxes")
ROUTE_COLUMNS = [
    ("program", "Program"),
    ("miles", "Miles"),
    ("taxes", "Taxes/Fees"),
    ("cabin", "Cabin"),
    ("seats", "Seats"),
    ("origin", "Origin"),
    ("destination", "Destination"),
    ("dates", "Dates"),
    ("routing", "Routing"),
]
SUBROUTE_COLUMNS = [
    ("program", "Program"),
    ("cabin", "Cabin"),
    ("miles", "Miles"),
    ("taxes", "Taxes/Fees"),
    ("seats", "Seats"),
    ("dates", "Dates"),
    ("routing", "Routing"),
]
SELF_CONNECT_COLUMNS = [
    ("direction", "Direction"),
    ("hub", "Via"),
    ("total_miles", "Total Miles"),
    ("leg1_program", "Leg 1 Program"),
    ("leg1_cabin", "Leg 1 Cabin"),
    ("leg1_miles", "Leg 1 Miles"),
    ("leg1_taxes", "Leg 1 Taxes"),
    ("leg1_depart_time", "Leg 1 Departs"),
    ("leg1_arrive_time", "Leg 1 Arrives"),
    ("layover_min", "Layover (min)"),
    ("leg2_program", "Leg 2 Program"),
    ("leg2_cabin", "Leg 2 Cabin"),
    ("leg2_miles", "Leg 2 Miles"),
    ("leg2_taxes", "Leg 2 Taxes"),
    ("leg2_depart_time", "Leg 2 Departs"),
    ("leg2_arrive_time", "Leg 2 Arrives"),
    ("seats", "Min Seats"),
    ("dates", "Dates"),
]


def build_route_report(raw_rows):
    """Shared pipeline for reports 1-3: explode -> dedupe -> sort by miles."""
    records = explode_rows(raw_rows)
    records = collapse_duplicate_routes(records, ROUTE_DEDUPE_KEY)
    records.sort(key=lambda r: (r["miles"] is None, r["miles"]))
    return records


def group_by_route(records, top_n):
    """Group already-deduplicated price-point records by (origin,
    destination) so each physical route appears once, with its different
    price points (program/cabin/miles combos) nested underneath as "sub
    routes" instead of each being its own top-level row.

    Returns the `top_n` cheapest routes (ranked by each route's own
    cheapest sub route), each as a dict with "origin", "destination", and
    "subroutes" (sorted cheapest-first).
    """
    routes = {}
    for r in records:
        key = (r["origin"], r["destination"])
        routes.setdefault(key, []).append(r)

    grouped = []
    for (origin, destination), subroutes in routes.items():
        subroutes.sort(key=lambda r: (r["miles"] is None, r["miles"]))
        grouped.append({
            "origin": origin,
            "destination": destination,
            "cheapest_miles": subroutes[0]["miles"],
            "subroutes": subroutes,
        })

    grouped.sort(key=lambda g: (g["cheapest_miles"] is None, g["cheapest_miles"]))
    return grouped[:top_n]


# ---------------------------------------------------------------------------
# CSV export - both as strings (for web downloads) and files (for the CLI)
# ---------------------------------------------------------------------------

def route_records_to_csv_string(records, columns=ROUTE_COLUMNS):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c[1] for c in columns])
    for r in records:
        writer.writerow([r[c[0]] for c in columns])
    return buf.getvalue()


def grouped_routes_to_csv_string(grouped, columns=SUBROUTE_COLUMNS):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Route"] + [c[1] for c in columns])
    for route in grouped:
        route_label = f"{route['origin']} -> {route['destination']}"
        for j, sub in enumerate(route["subroutes"]):
            writer.writerow(
                [route_label if j == 0 else ""] + [sub[c[0]] for c in columns]
            )
    return buf.getvalue()


def write_csv_file(csv_string, path, row_count, label="rows"):
    with open(path, "w", newline="") as f:
        f.write(csv_string)
    print(f"[csv] Wrote {row_count} {label} to {path}")


# ---------------------------------------------------------------------------
# Report 1: TPE arrivals from US gateways, Dec 16-21 2026
# ---------------------------------------------------------------------------

def build_report1_data(api_key, refresh=False):
    origin_str = ",".join(REPORT1_ORIGINS)
    cache_key = f"report1_{REPORT1_DESTINATION}_{REPORT1_START}_{REPORT1_END}"

    raw_rows = cached_search(
        api_key, origin_airport=origin_str, destination_airport=REPORT1_DESTINATION,
        start_date=REPORT1_START, end_date=REPORT1_END,
        cache_key=cache_key, refresh=refresh,
    )
    records = build_route_report(raw_rows)
    premium_records = [r for r in records if r["premium_cabin"]]

    return {
        "title": "TPE arrivals from US gateways",
        "start_date": REPORT1_START,
        "end_date": REPORT1_END,
        "records": records,
        "premium_records": premium_records,
        "generated_at": latest_cache_timestamp(cache_key),
    }


# ---------------------------------------------------------------------------
# Report 2: cheapest award routes from RIC/IAD, next 30 days
# ---------------------------------------------------------------------------

def build_report2_data(api_key, refresh=False):
    today = date.today()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=30)).isoformat()

    origin_str = ",".join(REPORT2_ORIGINS)
    destination_str = ",".join(REPORT2_DESTINATIONS)
    cache_key = f"report2_{start_date}_{end_date}"

    raw_rows = cached_search(
        api_key, origin_airport=origin_str, destination_airport=destination_str,
        start_date=start_date, end_date=end_date,
        cache_key=cache_key, refresh=refresh,
    )
    records = build_route_report(raw_rows)
    # One row per unique (origin, destination) route, with its different
    # program/cabin/price combinations nested underneath as sub routes.
    grouped_routes = group_by_route(records, REPORT2_TOP_N)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "grouped_routes": grouped_routes,
        "generated_at": latest_cache_timestamp(cache_key),
    }


# ---------------------------------------------------------------------------
# Report 3: TPE back to North America, Dec 29 2026 - Jan 3 2027
# ---------------------------------------------------------------------------

def build_report3_data(api_key, refresh=False):
    destination_str = ",".join(REPORT3_DESTINATIONS)
    cache_key = f"report3_{REPORT3_ORIGIN}_{REPORT3_START}_{REPORT3_END}"

    raw_rows = cached_search(
        api_key, origin_airport=REPORT3_ORIGIN, destination_airport=destination_str,
        start_date=REPORT3_START, end_date=REPORT3_END,
        cache_key=cache_key, refresh=refresh,
    )
    records = build_route_report(raw_rows)
    premium_records = [r for r in records if r["premium_cabin"]]

    return {
        "title": "TPE back to North America",
        "start_date": REPORT3_START,
        "end_date": REPORT3_END,
        "records": records,
        "premium_records": premium_records,
        "generated_at": latest_cache_timestamp(cache_key),
    }


# ---------------------------------------------------------------------------
# Report 4: RIC <-> SMF self-connect deals, next 3 months, 1 stop, <=3h
# ---------------------------------------------------------------------------

def find_same_day_connections(leg1_records, leg2_records):
    """Pair up leg1 (origin->hub) and leg2 (hub->destination) records that
    share a hub airport and travel date. Returns a list of (leg1, leg2)
    candidate pairs, cheapest combined mileage first.

    This is the "browse and add them up yourself" step, automated: we don't
    ask the API for RIC->SMF directly (it likely has no cached rows for
    that specific pair - it's not a route people commonly search). Instead
    we find every RIC->HUB row and every HUB->SMF row and match same-date
    pairs that share a hub.
    """
    leg2_by_hub_date = {}
    for r in leg2_records:
        leg2_by_hub_date.setdefault((r["origin"], r["date"]), []).append(r)

    pairs = []
    for leg1 in leg1_records:
        for leg2 in leg2_by_hub_date.get((leg1["destination"], leg1["date"]), []):
            pairs.append((leg1, leg2))

    pairs.sort(key=lambda p: p[0]["miles"] + p[1]["miles"])
    return pairs


def verify_layover(api_key, leg1, leg2, refresh):
    """Look up real flight times for both legs (via Get Trips) and check
    for at least one nonstop-per-leg combination with a layover between
    SELF_CONNECT_MIN_LAYOVER_MIN and SELF_CONNECT_MAX_LAYOVER_MIN minutes.

    Returns a dict describing the best valid connection found, or None if
    no combination of real flight times works.
    """
    leg1_options = [o for o in get_trip_options(api_key, leg1["id"], refresh)
                    if o["stops"] == 0]
    leg2_options = [o for o in get_trip_options(api_key, leg2["id"], refresh)
                    if o["stops"] == 0]

    best = None
    for opt1 in leg1_options:
        for opt2 in leg2_options:
            layover_min = (opt2["departs_at"] - opt1["arrives_at"]).total_seconds() / 60
            if SELF_CONNECT_MIN_LAYOVER_MIN <= layover_min <= SELF_CONNECT_MAX_LAYOVER_MIN:
                if best is None or layover_min < best["layover_min"]:
                    best = {
                        "layover_min": round(layover_min),
                        "leg1_departs": opt1["departs_at"],
                        "leg1_arrives": opt1["arrives_at"],
                        "leg2_departs": opt2["departs_at"],
                        "leg2_arrives": opt2["arrives_at"],
                    }
    return best


def build_report4_data(api_key, refresh=False):
    today = date.today()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=90)).isoformat()
    hub_str = ",".join(SELF_CONNECT_HUBS)
    origin_a, origin_b = SELF_CONNECT_AIRPORTS

    cache_keys = []

    def search(origin, destination, tag):
        cache_key = f"report4_{tag}_{start_date}_{end_date}"
        cache_keys.append(cache_key)
        raw = cached_search(api_key, origin, destination, start_date, end_date,
                             cache_key=cache_key, refresh=refresh)
        return explode_rows(raw)

    # Four cached-search calls: each direction's two legs.
    a_to_hub = search(origin_a, hub_str, f"{origin_a}_to_hubs")
    hub_to_b = search(hub_str, origin_b, f"hubs_to_{origin_b}")
    b_to_hub = search(origin_b, hub_str, f"{origin_b}_to_hubs")
    hub_to_a = search(hub_str, origin_a, f"hubs_to_{origin_a}")

    candidates = (
        [("->".join(SELF_CONNECT_AIRPORTS), p) for p in find_same_day_connections(a_to_hub, hub_to_b)]
        + [("->".join(reversed(SELF_CONNECT_AIRPORTS)), p) for p in find_same_day_connections(b_to_hub, hub_to_a)]
    )
    candidates.sort(key=lambda c: c[1][0]["miles"] + c[1][1]["miles"])

    print(f"\n[info] Found {len(candidates)} same-day leg-pair candidates via "
          f"{len(SELF_CONNECT_HUBS)} hub airports; checking real flight times "
          f"for the cheapest {min(len(candidates), SELF_CONNECT_MAX_LOOKUPS)} "
          f"of them (Get Trips lookups, cached per flight so re-runs are free).")

    results = []
    checked = 0
    for direction, (leg1, leg2) in candidates:
        if checked >= SELF_CONNECT_MAX_LOOKUPS:
            break
        checked += 1
        connection = verify_layover(api_key, leg1, leg2, refresh)
        if connection is None:
            continue
        results.append({
            "direction": direction,
            "hub": leg1["destination"],
            "leg1_program": leg1["program"],
            "leg1_cabin": leg1["cabin"],
            "leg1_miles": leg1["miles"],
            "leg1_taxes": leg1["taxes"],
            "leg2_program": leg2["program"],
            "leg2_cabin": leg2["cabin"],
            "leg2_miles": leg2["miles"],
            "leg2_taxes": leg2["taxes"],
            "total_miles": leg1["miles"] + leg2["miles"],
            "seats": min(leg1["seats"] or 0, leg2["seats"] or 0),
            "layover_min": connection["layover_min"],
            "leg1_depart_time": connection["leg1_departs"].strftime("%H:%M"),
            "leg1_arrive_time": connection["leg1_arrives"].strftime("%H:%M"),
            "leg2_depart_time": connection["leg2_departs"].strftime("%H:%M"),
            "leg2_arrive_time": connection["leg2_arrives"].strftime("%H:%M"),
            "date": leg1["date"],
        })

    dedupe_key = ("direction", "hub", "leg1_program", "leg1_cabin", "leg1_miles",
                  "leg2_program", "leg2_cabin", "leg2_miles", "layover_min")
    results = collapse_duplicate_routes(results, dedupe_key)
    results.sort(key=lambda r: r["total_miles"])

    return {
        "start_date": start_date,
        "end_date": end_date,
        "results": results,
        "candidates_total": len(candidates),
        "candidates_checked": checked,
        "generated_at": latest_cache_timestamp(cache_keys),
    }
