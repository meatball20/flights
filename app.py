#!/usr/bin/env python3
"""
Seats.aero award availability dashboard - web version.

A small Flask app that serves the same four reports as seats_aero_report.py
(the terminal tool) as web pages with a URL you can open, bookmark, and
reload. Each report page has its own "Refresh now" button that re-queries
the Seats.aero API on demand - all the actual fetching/parsing logic lives
in seats_aero_core.py, which this file just renders as HTML instead of
printing to a terminal.

Run locally:
    python app.py
    # then open http://localhost:5000

Deploy: see the "Hosting this as a website" section in README.md for exact
steps to put this on Render's free tier with a public URL.

SECURITY NOTE: this page can trigger real Seats.aero API calls (spending
your daily quota) and shows your personal travel search data. If you
deploy it publicly, set DASHBOARD_USERNAME and DASHBOARD_PASSWORD (see
below) so random visitors with the URL can't load it or burn your quota.
"""

import os
import traceback

from flask import Flask, Response, abort, redirect, render_template, request, url_for

import seats_aero_core as core

app = Flask(__name__)

# Fails fast (with a clear message) at startup if the key isn't configured,
# same as the CLI tool - see seats_aero_core.get_api_key().
API_KEY = core.get_api_key()

# Optional HTTP Basic Auth. If you deploy this publicly, set both of these
# as environment variables (never hardcode them) so the dashboard isn't
# wide open to anyone who finds the URL. Left unset, the app runs without
# auth - fine for local-only use, not recommended once it's on the internet.
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")


def require_auth(view):
    """Wrap a view in HTTP Basic Auth, but only if both env vars are set."""
    if not (DASHBOARD_USERNAME and DASHBOARD_PASSWORD):
        return view

    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USERNAME or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Login required.", 401,
                {"WWW-Authenticate": 'Basic realm="Seats.aero dashboard"'},
            )
        return view(*args, **kwargs)

    wrapped.__name__ = view.__name__
    return wrapped


# Central place describing each report: its data-builder function from
# seats_aero_core, which HTML template renders it, and a short name for nav
# links. Adding a 5th report later just means adding one entry here.
REPORTS = {
    1: {
        "name": "TPE Arrivals",
        "builder": core.build_report1_data,
        "template": "route_report.html",
    },
    2: {
        "name": "RIC/IAD Cheapest",
        "builder": core.build_report2_data,
        "template": "report2.html",
    },
    3: {
        "name": "TPE Return",
        "builder": core.build_report3_data,
        "template": "route_report.html",
    },
    4: {
        "name": "RIC ↔ SMF Self-Connect",
        "builder": core.build_report4_data,
        "template": "report4.html",
    },
}


@app.route("/")
@require_auth
def index():
    return render_template("index.html", reports=REPORTS)


@app.route("/report/<int:report_id>")
@require_auth
def report(report_id):
    if report_id not in REPORTS:
        abort(404)
    refresh = request.args.get("refresh") == "1"

    error = None
    data = None
    try:
        data = REPORTS[report_id]["builder"](API_KEY, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 - shown to you, the only viewer
        traceback.print_exc()  # full detail goes to the server log
        error = str(exc)

    if refresh and error is None:
        # Refresh succeeded: redirect to the plain URL so a browser reload
        # afterward re-reads the (now fresh) cache instead of silently
        # spending another day's-quota API call every time you hit F5.
        return redirect(url_for("report", report_id=report_id))

    return render_template(
        REPORTS[report_id]["template"],
        report_id=report_id,
        name=REPORTS[report_id]["name"],
        data=data,
        error=error,
        route_columns=core.ROUTE_COLUMNS,
        subroute_columns=core.SUBROUTE_COLUMNS,
        self_connect_columns=core.SELF_CONNECT_COLUMNS,
        min_layover=core.SELF_CONNECT_MIN_LAYOVER_MIN,
        max_layover=core.SELF_CONNECT_MAX_LAYOVER_MIN,
    )


@app.route("/report/<int:report_id>/csv")
@require_auth
def report_csv(report_id):
    if report_id not in REPORTS:
        abort(404)
    # Never force a refresh just for a CSV download - use whatever's
    # already cached (or do one fresh fetch if nothing's cached yet).
    data = REPORTS[report_id]["builder"](API_KEY, refresh=False)

    if report_id == 2:
        csv_text = core.grouped_routes_to_csv_string(data["grouped_routes"])
    elif report_id == 4:
        csv_text = core.route_records_to_csv_string(data["results"], core.SELF_CONNECT_COLUMNS)
    else:
        csv_text = core.route_records_to_csv_string(data["records"])

    filename = f"seats_aero_report{report_id}.csv"
    return Response(
        csv_text, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    # Local dev server. Render (and most hosts) set $PORT for you; default
    # to 5000 for running this on your own machine.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
