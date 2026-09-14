#!/usr/bin/env python3
"""
Seats.aero award availability reporter (Pro/cached-data tier).

Runs two reports against the Seats.aero Partner API "Cached Search" endpoint
(GET /partnerapi/search) and prints readable tables + writes CSVs:

  Report 1 - Flights arriving TPE from major US gateways, Dec 16-21 2026.
  Report 2 - Cheapest award routes departing RIC or IAD in the next 30 days.

Why Cached Search (and not Bulk Availability) for both reports
----------------------------------------------------------------
Seats.aero's Partner API has two cached-data endpoints:

  - Cached Search  (GET /partnerapi/search)      -> one or more origins,
    one or more destinations, across ALL mileage programs, in one call.
  - Bulk Availability (GET /partnerapi/availability) -> ALL routes for ONE
    mileage program at a time (filtered by broad origin/destination
    *region*, not by specific airport).

Both reports here ask for a handful of specific origin airports searched
against every mileage program at once, which is exactly what Cached Search
is built for. Bulk Availability would need one API call per mileage program
(there are ~25+) and doesn't accept a specific-airport filter, so it would
burn far more of your daily quota for a worse fit. That's a Pro-tier
distinction, confirmed against the public API docs/reference before writing
any code - see the note at the bottom of this file for links.

Live Search (real-time, commercial-only) is never used here - Pro keys only
get cached/bulk data, per the task requirements.
"""

import csv
import json
import os
import sys
import time
from datetime import date, timedelta
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
        "Date": "2026-12-16",
        "Route": {"OriginAirport": "SFO", "DestinationAirport": "TPE"},
        "Source": "united",
        "YAvailable": true, "YMileageCostRaw": 70000, "YRemainingSeats": 2,
        "YTotalTaxes": 5600, "TaxesCurrency": "USD", "YDirect": true,
        ... (same pattern repeated for W, J, F) ...
      }
    We flatten that into up to 4 separate records, one per available cabin.
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
    records = explode_rows(raw_rows)
    records.sort(key=lambda r: (r["miles"] is None, r["miles"]))

    columns = [
        ("program", "Program"),
        ("miles", "Miles"),
        ("taxes", "Taxes/Fees"),
        ("cabin", "Cabin"),
        ("seats", "Seats"),
        ("origin", "Origin"),
        ("date", "Date"),
        ("routing", "Routing"),
    ]

    print_table(records, columns, "REPORT 1: All cabins - TPE arrivals from US gateways (Dec 16-21, 2026)")
    write_csv(records, columns, OUTPUT_DIR / "report1_tpe_all_cabins.csv")

    premium_records = [r for r in records if r["premium_cabin"]]
    print_table(premium_records, columns,
                "REPORT 1: Business/First only - TPE arrivals from US gateways")
    write_csv(premium_records, columns, OUTPUT_DIR / "report1_tpe_business_first.csv")


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
    records = explode_rows(raw_rows)
    records.sort(key=lambda r: (r["miles"] is None, r["miles"]))
    top_records = records[:REPORT2_TOP_N]

    columns = [
        ("destination", "Destination"),
        ("date", "Date"),
        ("miles", "Miles"),
        ("taxes", "Taxes/Fees"),
        ("cabin", "Cabin"),
        ("program", "Program"),
        ("seats", "Seats"),
        ("origin", "Origin"),
        ("routing", "Routing"),
    ]

    print_table(top_records, columns,
                f"REPORT 2: Top {REPORT2_TOP_N} cheapest routes from RIC/IAD "
                f"({start_date} to {end_date})")
    # The CSV holds everything we found, not just the printed top N, in case
    # you want to explore beyond the top 25 later without re-querying.
    write_csv(records, columns, OUTPUT_DIR / "report2_ric_iad_cheapest.csv")


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

    print(f"\nDone. CSVs written to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Docs consulted (Pro/cached-tier endpoints only; Live Search intentionally
# not used - it's commercial-only and rejects Pro keys):
#   https://developers.seats.aero/reference/cached-search
#   https://developers.seats.aero/reference/get-availability   (Bulk Availability)
#   https://developers.seats.aero/reference/getting-started-p  (auth header)
#   https://docs.seats.aero/article/68-seatsaero-pro-api-access-limits-and-usage
# ---------------------------------------------------------------------------
