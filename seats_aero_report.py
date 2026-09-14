#!/usr/bin/env python3
"""
Seats.aero award availability reporter - terminal version.

Runs the four reports (see seats_aero_core.py for what each one does and
the research behind them) and prints readable tables + writes CSVs to
output/. All the actual API/parsing logic lives in seats_aero_core.py -
this file is just the terminal presentation layer. The web dashboard
(app.py) is the other presentation layer over the same core module.

Usage:
  python seats_aero_report.py             # use cached data where available
  python seats_aero_report.py --refresh   # ignore cache, re-query the API
"""

import sys

from tabulate import tabulate

import seats_aero_core as core


def print_table(records, columns, title):
    print(f"\n{'=' * len(title)}\n{title}\n{'=' * len(title)}")
    if not records:
        print("(no availability found)")
        return
    headers = [c[1] for c in columns]
    rows = [[r[c[0]] for c in columns] for r in records]
    print(tabulate(rows, headers=headers, tablefmt="github"))


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


def run_report1(api_key, refresh):
    data = core.build_report1_data(api_key, refresh)
    title = (f"REPORT 1: All cabins - {data['title']} "
             f"({data['start_date']} to {data['end_date']})")
    print_table(data["records"], core.ROUTE_COLUMNS, title)
    core.write_csv_file(
        core.route_records_to_csv_string(data["records"]),
        core.OUTPUT_DIR / "report1_tpe_all_cabins.csv",
        len(data["records"]),
    )

    print_table(data["premium_records"], core.ROUTE_COLUMNS,
                "REPORT 1: Business/First only - TPE arrivals from US gateways")
    core.write_csv_file(
        core.route_records_to_csv_string(data["premium_records"]),
        core.OUTPUT_DIR / "report1_tpe_business_first.csv",
        len(data["premium_records"]),
    )


def run_report2(api_key, refresh):
    data = core.build_report2_data(api_key, refresh)
    print_grouped_routes(
        data["grouped_routes"], core.SUBROUTE_COLUMNS,
        f"REPORT 2: Top {core.REPORT2_TOP_N} cheapest DISTINCT routes from "
        f"RIC/IAD ({data['start_date']} to {data['end_date']})",
    )
    row_count = sum(len(g["subroutes"]) for g in data["grouped_routes"])
    core.write_csv_file(
        core.grouped_routes_to_csv_string(data["grouped_routes"]),
        core.OUTPUT_DIR / "report2_ric_iad_cheapest.csv",
        row_count, label="price-point rows",
    )


def run_report3(api_key, refresh):
    data = core.build_report3_data(api_key, refresh)
    title = (f"REPORT 3: All cabins - {data['title']} "
             f"({data['start_date']} to {data['end_date']})")
    print_table(data["records"], core.ROUTE_COLUMNS, title)
    core.write_csv_file(
        core.route_records_to_csv_string(data["records"]),
        core.OUTPUT_DIR / "report3_tpe_return_all_cabins.csv",
        len(data["records"]),
    )

    print_table(data["premium_records"], core.ROUTE_COLUMNS,
                "REPORT 3: Business/First only - TPE back to North America")
    core.write_csv_file(
        core.route_records_to_csv_string(data["premium_records"]),
        core.OUTPUT_DIR / "report3_tpe_return_business_first.csv",
        len(data["premium_records"]),
    )


def run_report4(api_key, refresh):
    data = core.build_report4_data(api_key, refresh)
    print_table(
        data["results"], core.SELF_CONNECT_COLUMNS,
        f"REPORT 4: RIC <-> SMF self-connect deals, 1 stop, "
        f"{core.SELF_CONNECT_MIN_LAYOVER_MIN}-{core.SELF_CONNECT_MAX_LAYOVER_MIN} min "
        f"layover ({data['start_date']} to {data['end_date']})",
    )
    print("\n  NOTE: these are two separately-ticketed award bookings you'd "
          "connect yourself, not one protected itinerary. If leg 1 is "
          "delayed, leg 2 isn't held for you and isn't refunded automatically. "
          "Departure/arrival times shown are time-of-day from a checked "
          "date; always re-verify the exact schedule for the date you book.")
    core.write_csv_file(
        core.route_records_to_csv_string(data["results"], core.SELF_CONNECT_COLUMNS),
        core.OUTPUT_DIR / "report4_ric_smf_self_connect.csv",
        len(data["results"]),
    )


def main():
    refresh = "--refresh" in sys.argv
    api_key = core.get_api_key()

    if refresh:
        print("[info] --refresh passed: ignoring any cached files and "
              "re-querying the API.")

    run_report1(api_key, refresh)
    run_report2(api_key, refresh)
    run_report3(api_key, refresh)
    run_report4(api_key, refresh)

    print(f"\nDone. CSVs written to: {core.OUTPUT_DIR}")


if __name__ == "__main__":
    main()
