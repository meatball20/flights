#!/usr/bin/env python3
"""
Seats.aero award availability reporter (Pro/cached-data tier).

Runs four reports against the Seats.aero Partner API and prints readable
tables + writes CSVs:

  Report 1 - Flights arriving TPE from major US gateways, Dec 16-21 2026.
  Report 2 - Cheapest award routes departing RIC or IAD in the next 30 days.
  Report 3 - Flights from TPE back to North America, Dec 29 2026 - Jan 3 2027.
  Report 4 - RIC <-> SMF self-connect deals (1 stop, <=3h layover) in the
             next 3 months.

Why Cached Search (and not Bulk Availability) for reports 1-3
----------------------------------------------------------------
Seats.aero's Partner API has two cached-data endpoints:

  - Cached Search  (GET /partnerapi/search)      -> one or more origins,
    one or more destinations, across ALL mileage programs, in one call.
  - Bulk Availability (GET /partnerapi/availability) -> ALL routes for ONE
    mileage program at a time (filtered by broad origin/destination
    *region*, not by specific airport).

Reports 1-3 ask for a handful of specific origin airports searched against
every mileage program at once, which is exactly what Cached Search is built
for. Bulk Availability would need one API call per mileage program (there
are ~25+) and doesn't accept a specific-airport filter, so it would burn far
more of your daily quota for a worse fit.

Report 4 needs something extra: actual flight *times*, so it can check a
real layover window. Cached Search's summary rows don't include times, so
for that report we also call the "Get Trips" endpoint (GET
/partnerapi/trips/{id}) which returns real departure/arrival times per
flight segment for a given availability row. See run_report4() below for
how that's used.

Live Search (real-time, commercial-only) is never used here - Pro keys only
get cached/bulk data, per the task requirements.

Docs consulted (Pro/cached-tier endpoints only):
  https://developers.seats.aero/reference/cached-search
  https://developers.seats.aero/reference/get-availability   (Bulk Availability)
  https://developers.seats.aero/reference/get-trips
  https://developers.seats.aero/reference/getting-started-p  (auth header)
  https://docs.seats.aero/article/68-seatsaero-pro-api-access-limits-and-usage
"""

import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Setup: paths, constants, and the .env file
# ---------------------------------------------------------------------------

# Everything lives next to this script so it works no matter where you run
# `python seats_aero_report.py` from.
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
OUTPUT_DIR = SCRIPT_DIR / "output"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# python-dotenv reads a .env file (if present) and loads its KEY=VALUE lines
# into the environment, so SEATS_AERO_API_KEY can live in a local .env file
# instead of being typed into your shell every time.
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

# Report 1: major US gateway airports flying to Asia. The task's required
# list plus a handful more common TPE-relevant US gateways. Cached Search
# accepts a comma-separated list for origin_airport, so this is ONE API
# call (plus pagination), not eleven-plus separate ones.
REPORT1_ORIGINS = [
    "SFO", "LAX", "SEA", "SJC", "JFK", "EWR", "IAD", "IAH", "ORD", "DFW",
    "ONT", "ATL", "BOS", "MIA", "DEN", "LAS", "PHX", "HNL",
]
REPORT1_DESTINATION = "TPE"
REPORT1_START = "2026-12-16"
REPORT1_END = "2026-12-21"

# Report 2: Cached Search requires a destination_airport value - there's no
# "search everywhere" wildcard on the cached endpoint. To approximate
# "any destination" we search against a broad list of major airports
# worldwide (the same kind of gateway list seats.aero's own multi-city
# shortcuts like "USA" or "EUR" expand to). This won't catch every possible
# obscure destination, but it covers the routes with realistically useful
# award availability.
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

# Report 3: the return leg - TPE back to North America. Reuses report 1's
# gateway list (same US airports, now as destinations) plus a few Canada/
# Mexico gateways, for the same "no true wildcard, so use a broad curated
# list" reason as report 2.
REPORT3_ORIGIN = "TPE"
REPORT3_DESTINATIONS = REPORT1_ORIGINS + ["YYZ", "YVR", "YUL", "YYC", "MEX", "CUN"]
REPORT3_START = "2026-12-29"
REPORT3_END = "2027-01-03"

# Report 4: RIC <-> SMF is a small-airport pair that seats.aero's cached
# database usually won't have a direct row for (it only caches the route
# pairs its crawler actually searches, which skews toward routes people
# commonly search). So instead of searching RIC->SMF directly, we search
# RIC and SMF each against a list of major US hub airports that plausibly
# connect them, then match up same-day legs ourselves - i.e. exactly the
# "browse and add them up yourself" approach, automated.
SELF_CONNECT_AIRPORTS = ("RIC", "SMF")
SELF_CONNECT_HUBS = [
    "ORD", "DFW", "ATL", "DEN", "IAH", "CLT", "PHX", "SLC", "MSP", "LAS",
    "SEA", "EWR", "JFK", "PHL", "DTW", "LAX", "SFO",
]
SELF_CONNECT_MAX_LAYOVER_MIN = 180  # your "3 hours is the longest" rule
# Not requested, but added as a sanity floor: a same-day self-transfer under
# ~45 minutes generally isn't realistically bookable/safe (these are two
# *separate* award tickets, not one protected itinerary - no rebooking if
# leg 1 runs late). Change/remove SELF_CONNECT_MIN_LAYOVER_MIN if you'd
# rather see those too.
SELF_CONNECT_MIN_LAYOVER_MIN = 45
# How many of the cheapest same-day leg-pairs we'll spend extra API calls on
# to verify with real flight times (Get Trips costs one call per leg looked
# up). Keeps quota use predictable even if there turn out to be hundreds of
# candidate date/hub combinations.
SELF_CONNECT_MAX_LOOKUPS = 60


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------

def get_api_key():
    """Read the API key from the environment (or a .env file), or exit."""
    api_key = os.environ.get("SEATS_AERO_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: SEATS_AERO_API_KEY is not set.\n"
            "Either export it in your shell, or create a .env file next to "
            "this script (copy .env.example to .env and fill in your key)."
        )
    return api_key


def log_remaining_quota(response):
    """Print how many API calls are left today, from the response headers.

    Seats.aero returns your remaining daily quota in a rate-limit header.
    `requests` headers are case-insensitive, so this works regardless of
    exactly how the header name is capitalized on the wire.
    """
    remaining = response.headers.get("X-RateLimit-Remaining")
    limit = response.headers.get("X-RateLimit-Limit")
    if remaining is not None:
        print(f"  [quota] API calls remaining today: {remaining}"
              + (f" / {limit}" if limit else ""))
    else:
        # Fall back to scanning all headers in case the exact name differs.
        rate_headers = {k: v for k, v in response.headers.items()
                         if "ratelimit" in k.lower() or "remaining" in k.lower()}
        if rate_headers:
            print(f"  [quota] rate-limit headers seen: {rate_headers}")


def cached_search(api_key, origin_airport, destination_airport, start_date,
                   end_date, cache_key, refresh=False):
    """Call Cached Search (GET /partnerapi/search) and return all rows.

    Handles pagination (cursor/hasMore) and caches the combined raw JSON
    response to a local file so re-running the script for analysis-only
    changes doesn't re-hit the API or spend quota.
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
            sys.exit(
                f"ERROR: Seats.aero API returned {response.status_code} "
                f"for {response.url}\n{response.text[:500]}"
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


# ---------------------------------------------------------------------------
# Turning raw API rows into per-cabin records we can sort/print/export
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
# Printing and CSV export
# ---------------------------------------------------------------------------

def print_table(records, columns, title):
    print(f"\n{'=' * len(title)}\n{title}\n{'=' * len(title)}")
    if not records:
        print("(no availability found)")
        return
    headers = [c[1] for c in columns]
    rows = [[r[c[0]] for c in columns] for r in records]
    print(tabulate(rows, headers=headers, tablefmt="github"))


def write_csv(records, columns, path):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([c[1] for c in columns])
        for r in records:
            writer.writerow([r[c[0]] for c in columns])
    print(f"[csv] Wrote {len(records)} rows to {path}")


# ---------------------------------------------------------------------------
# Grouping price points under one row per physical route (origin+destination)
# ---------------------------------------------------------------------------

SUBROUTE_COLUMNS = [
    ("program", "Program"),
    ("cabin", "Cabin"),
    ("miles", "Miles"),
    ("taxes", "Taxes/Fees"),
    ("seats", "Seats"),
    ("dates", "Dates"),
    ("routing", "Routing"),
]


def group_by_route(records, top_n):
    """Group already-deduplicated price-point records by (origin,
    destination) so each physical route appears once, with its different
    price points (program/cabin/miles combos) nested underneath as "sub
    routes" instead of each being its own top-level row.

    `records` is expected to already be the output of
    collapse_duplicate_routes() - i.e. one record per unique route+program+
    cabin+price, dates already combined. This just does one more level of
    grouping on top of that: by route alone.

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


def print_grouped_routes(grouped, subroute_columns, title):
    """Print one route per group, with its sub routes tab-indented beneath
    it - the "route [tab] sub routes" layout."""
    print(f"\n{'=' * len(title)}\n{title}\n{'=' * len(title)}")
    if not grouped:
        print("(no availability found)")
        return
    for i, route in enumerate(grouped, start=1):
        print(f"\n{i}. {route['origin']} -> {route['destination']} "
              f"(cheapest: {route['cheapest_miles']:,} miles, "
              f"{len(route['subroutes'])} price point"
              f"{'s' if len(route['subroutes']) != 1 else ''})")
        headers = [c[1] for c in subroute_columns]
        rows = [[sub[c[0]] for c in subroute_columns] for sub in route["subroutes"]]
        table = tabulate(rows, headers=headers, tablefmt="github")
        for line in table.splitlines():
            print(f"\t{line}")


def write_grouped_csv(grouped, subroute_columns, path):
    """Write one CSV row per sub route, with a leading Route column that's
    only filled in on each route's first row (blank on the rest) - the
    same "route, then its sub routes underneath" grouping, spreadsheet
    style."""
    row_count = 0
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Route"] + [c[1] for c in subroute_columns])
        for route in grouped:
            route_label = f"{route['origin']} -> {route['destination']}"
            for j, sub in enumerate(route["subroutes"]):
                writer.writerow(
                    [route_label if j == 0 else ""]
                    + [sub[c[0]] for c in subroute_columns]
                )
                row_count += 1
    print(f"[csv] Wrote {len(grouped)} routes ({row_count} price-point rows) to {path}")


# Standard column layout shared by reports 1-3 (all deduplicated, one row
# per unique route+price with a combined "dates" column).
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


def build_route_report(raw_rows, sort_and_dedupe=True):
    """Shared pipeline for reports 1-3: explode -> dedupe -> sort by miles."""
    records = explode_rows(raw_rows)
    records = collapse_duplicate_routes(records, ROUTE_DEDUPE_KEY)
    if sort_and_dedupe:
        records.sort(key=lambda r: (r["miles"] is None, r["miles"]))
    return records


# ---------------------------------------------------------------------------
# Report 1: TPE arrivals from US gateways, Dec 16-21 2026
# ---------------------------------------------------------------------------

def run_report1(api_key, refresh):
    origin_str = ",".join(REPORT1_ORIGINS)
    cache_key = f"report1_{REPORT1_DESTINATION}_{REPORT1_START}_{REPORT1_END}"

    raw_rows = cached_search(
        api_key,
        origin_airport=origin_str,
        destination_airport=REPORT1_DESTINATION,
        start_date=REPORT1_START,
        end_date=REPORT1_END,
        cache_key=cache_key,
        refresh=refresh,
    )
    records = build_route_report(raw_rows)

    print_table(records, ROUTE_COLUMNS,
                "REPORT 1: All cabins - TPE arrivals from US gateways (Dec 16-21, 2026)")
    write_csv(records, ROUTE_COLUMNS, OUTPUT_DIR / "report1_tpe_all_cabins.csv")

    premium_records = [r for r in records if r["premium_cabin"]]
    print_table(premium_records, ROUTE_COLUMNS,
                "REPORT 1: Business/First only - TPE arrivals from US gateways")
    write_csv(premium_records, ROUTE_COLUMNS, OUTPUT_DIR / "report1_tpe_business_first.csv")


# ---------------------------------------------------------------------------
# Report 2: cheapest award routes from RIC/IAD, next 30 days
# ---------------------------------------------------------------------------

def run_report2(api_key, refresh):
    today = date.today()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=30)).isoformat()

    origin_str = ",".join(REPORT2_ORIGINS)
    destination_str = ",".join(REPORT2_DESTINATIONS)
    cache_key = f"report2_{start_date}_{end_date}"

    raw_rows = cached_search(
        api_key,
        origin_airport=origin_str,
        destination_airport=destination_str,
        start_date=start_date,
        end_date=end_date,
        cache_key=cache_key,
        refresh=refresh,
    )
    records = build_route_report(raw_rows)
    # One row per unique (origin, destination) route, with its different
    # program/cabin/price combinations nested underneath as sub routes -
    # so e.g. RIC->LAX counts as ONE of the 25 routes even though it might
    # have 4 different mileage programs/cabins pricing it differently.
    grouped_routes = group_by_route(records, REPORT2_TOP_N)

    print_grouped_routes(
        grouped_routes, SUBROUTE_COLUMNS,
        f"REPORT 2: Top {REPORT2_TOP_N} cheapest DISTINCT routes from "
        f"RIC/IAD ({start_date} to {end_date})",
    )
    write_grouped_csv(grouped_routes, SUBROUTE_COLUMNS,
                       OUTPUT_DIR / "report2_ric_iad_cheapest.csv")


# ---------------------------------------------------------------------------
# Report 3: TPE back to North America, Dec 29 2026 - Jan 3 2027
# ---------------------------------------------------------------------------

def run_report3(api_key, refresh):
    destination_str = ",".join(REPORT3_DESTINATIONS)
    cache_key = f"report3_{REPORT3_ORIGIN}_{REPORT3_START}_{REPORT3_END}"

    raw_rows = cached_search(
        api_key,
        origin_airport=REPORT3_ORIGIN,
        destination_airport=destination_str,
        start_date=REPORT3_START,
        end_date=REPORT3_END,
        cache_key=cache_key,
        refresh=refresh,
    )
    records = build_route_report(raw_rows)

    print_table(records, ROUTE_COLUMNS,
                "REPORT 3: All cabins - TPE back to North America (Dec 29, 2026 - Jan 3, 2027)")
    write_csv(records, ROUTE_COLUMNS, OUTPUT_DIR / "report3_tpe_return_all_cabins.csv")

    premium_records = [r for r in records if r["premium_cabin"]]
    print_table(premium_records, ROUTE_COLUMNS,
                "REPORT 3: Business/First only - TPE back to North America")
    write_csv(premium_records, ROUTE_COLUMNS, OUTPUT_DIR / "report3_tpe_return_business_first.csv")


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
    # Index leg2 by (hub, date) for fast lookup while scanning leg1.
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


def run_report4(api_key, refresh):
    today = date.today()
    start_date = today.isoformat()
    end_date = (today + timedelta(days=90)).isoformat()
    hub_str = ",".join(SELF_CONNECT_HUBS)
    origin_a, origin_b = SELF_CONNECT_AIRPORTS

    def search(origin, destination, tag):
        cache_key = f"report4_{tag}_{start_date}_{end_date}"
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

    # Same idea as reports 1-3: the same connection (same flights, same
    # price) tends to repeat across many dates, so collapse those into one
    # row with a combined dates list instead of showing every date.
    dedupe_key = ("direction", "hub", "leg1_program", "leg1_cabin", "leg1_miles",
                  "leg2_program", "leg2_cabin", "leg2_miles", "layover_min")
    results = collapse_duplicate_routes(results, dedupe_key)
    results.sort(key=lambda r: r["total_miles"])

    columns = [
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

    print_table(results, columns,
                f"REPORT 4: RIC <-> SMF self-connect deals, 1 stop, "
                f"{SELF_CONNECT_MIN_LAYOVER_MIN}-{SELF_CONNECT_MAX_LAYOVER_MIN} min "
                f"layover ({start_date} to {end_date})")
    print("\n  NOTE: these are two separately-ticketed award bookings you'd "
          "connect yourself, not one protected itinerary. If leg 1 is "
          "delayed, leg 2 isn't held for you and isn't refunded automatically. "
          "Departure/arrival times shown are time-of-day from a checked "
          "date; always re-verify the exact schedule for the date you book.")
    write_csv(results, columns, OUTPUT_DIR / "report4_ric_smf_self_connect.csv")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    refresh = "--refresh" in sys.argv
    api_key = get_api_key()

    if refresh:
        print("[info] --refresh passed: ignoring any cached files and "
              "re-querying the API.")

    run_report1(api_key, refresh)
    run_report2(api_key, refresh)
    run_report3(api_key, refresh)
    run_report4(api_key, refresh)

    print(f"\nDone. CSVs written to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
