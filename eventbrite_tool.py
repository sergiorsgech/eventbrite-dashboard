#!/usr/bin/env python3
"""
Eventbrite API helper.

Uses Eventbrite's REST API v3 (https://www.eventbrite.com/platform/api) to pull
your organizations, events, and attendees, and to build a local HTML dashboard.

Setup:
  1. Get a Private Token from Eventbrite:
     Account Settings -> Developer Links -> API Keys -> Private Token
     (https://www.eventbrite.com/platform/api-keys/)
  2. Set it as an environment variable before running:
     export EVENTBRITE_TOKEN="your_private_token_here"

Usage:
  python3 eventbrite_tool.py orgs
  python3 eventbrite_tool.py events [--org ORG_ID] [--status all|live|draft|started|ended|completed|canceled]
  python3 eventbrite_tool.py attendees --event EVENT_ID
  python3 eventbrite_tool.py orders --event EVENT_ID
  python3 eventbrite_tool.py dashboard [--org ORG_ID] [--out eventbrite_dashboard.html]
"""

import argparse
import base64
import json
import os
import sys
import datetime
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://www.eventbriteapi.com/v3"


class ApiError(Exception):
    """Raised for a soft (non-fatal) API failure, e.g. per-event ticket stats."""
    pass


def get_token():
    token = os.environ.get("EVENTBRITE_TOKEN")
    if not token:
        sys.stderr.write(
            "ERROR: EVENTBRITE_TOKEN environment variable not set.\n"
            "Get your Private Token at https://www.eventbrite.com/platform/api-keys/\n"
            "then run: export EVENTBRITE_TOKEN=\"your_token\"\n"
        )
        sys.exit(1)
    return token


def api_get(path, token, params=None, soft=False):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if soft:
            raise ApiError(f"HTTP {e.code} calling {url}: {body}")
        sys.stderr.write(f"HTTP {e.code} error calling {url}:\n{body}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        if soft:
            raise ApiError(f"Connection error calling {url}: {e}")
        sys.stderr.write(f"Connection error calling {url}: {e}\n")
        sys.exit(1)


def paginate(path, token, params=None, item_key=None, soft=False):
    params = dict(params or {})
    results = []
    page = 1
    while True:
        params["page"] = page
        data = api_get(path, token, params, soft=soft)
        key = item_key or next(k for k in data if isinstance(data[k], list))
        results.extend(data[key])
        pagination = data.get("pagination", {})
        if not pagination.get("has_more_items"):
            break
        page += 1
    return results


def get_default_org(token, explicit_org=None):
    if explicit_org:
        return explicit_org
    orgs = api_get("/users/me/organizations/", token).get("organizations", [])
    if not orgs:
        return None
    return orgs[0]


def cmd_orgs(args, token):
    data = api_get("/users/me/organizations/", token)
    orgs = data.get("organizations", [])
    for org in orgs:
        print(f"{org['id']}\t{org['name']}")
    if not orgs:
        print("No organizations found for this account.")


def cmd_events(args, token):
    if args.org:
        org_id = args.org
    else:
        orgs = api_get("/users/me/organizations/", token).get("organizations", [])
        if not orgs:
            print("No organizations found; specify --org ORG_ID explicitly.")
            return
        org_id = orgs[0]["id"]

    params = {"order_by": "start_desc"}
    if args.status:
        params["status"] = args.status

    events = paginate(f"/organizations/{org_id}/events/", token, params, item_key="events")
    for ev in events:
        name = ev["name"]["text"]
        start = ev["start"]["local"]
        status = ev["status"]
        print(f"{ev['id']}\t{start}\t{status}\t{name}")
    if not events:
        print("No events found.")


def cmd_attendees(args, token):
    attendees = paginate(f"/events/{args.event}/attendees/", token, item_key="attendees")
    for a in attendees:
        profile = a.get("profile", {})
        name = profile.get("name", "Unknown")
        email = profile.get("email", "")
        status = a.get("status", "")
        ticket = a.get("ticket_class_name", "")
        print(f"{name}\t{email}\t{status}\t{ticket}")
    if not attendees:
        print("No attendees found for this event.")


def cmd_orders(args, token):
    orders = paginate(f"/events/{args.event}/orders/", token, item_key="orders")
    for o in orders:
        name = o.get("name", "")
        email = o.get("email", "")
        total = o.get("costs", {}).get("gross", {}).get("display", "")
        created = o.get("created", "")
        print(f"{created}\t{name}\t{email}\t{total}")
    if not orders:
        print("No orders found for this event.")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def get_event_ticket_stats(event_id, token):
    """Aggregate ticket/order/cancellation/check-in stats from an event's attendees.

    Returns a dict with tickets_total (gross attendee records), orders_total
    (unique order IDs), canceled (cancelled or refunded tickets), and
    checked_in (tickets scanned in). Returns zeros (and an error string) if
    the attendees endpoint can't be read for this event.
    """
    stats = {"tickets_total": 0, "orders_total": 0, "canceled": 0, "checked_in": 0, "error": None}
    try:
        attendees = paginate(f"/events/{event_id}/attendees/", token, item_key="attendees", soft=True)
    except ApiError as e:
        stats["error"] = str(e)
        return stats

    order_ids = set()
    for a in attendees:
        stats["tickets_total"] += 1
        order_id = a.get("order_id")
        if order_id:
            order_ids.add(order_id)
        if a.get("cancelled") or a.get("refunded"):
            stats["canceled"] += 1
        if a.get("checked_in"):
            stats["checked_in"] += 1
    stats["orders_total"] = len(order_ids)
    return stats


CACHE_FILE = ".eventbrite_ticket_cache.json"


def load_ticket_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_ticket_cache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass


def cmd_dashboard(args, token):
    org = get_default_org(token, args.org)
    if not org:
        print("No organizations found; specify --org ORG_ID explicitly.")
        return
    org_id = org if isinstance(org, str) else org["id"]
    org_name = org if isinstance(org, str) else org["name"]

    all_events = paginate(
        f"/organizations/{org_id}/events/",
        token,
        {"order_by": "start_desc"},
        item_key="events",
    )

    candidates = []
    excluded = 0
    for ev in all_events:
        status = ev.get("status", "")
        start_local = ev.get("start", {}).get("local", "")
        if not start_local:
            continue
        year = int(start_local[0:4])
        if year not in (2025, 2026):
            continue
        if status not in ("live", "completed"):
            excluded += 1
            continue
        month = int(start_local[5:7])
        candidates.append({
            "id": ev["id"],
            "name": ev["name"]["text"],
            "start": start_local,
            "year": year,
            "month": month,
            "status": status,
            "url": ev.get("url", f"https://www.eventbrite.com/e/{ev['id']}"),
        })

    candidates.sort(key=lambda r: r["start"], reverse=True)

    rows = []
    ticket_errors = []
    if args.tickets:
        cache = {} if args.refresh_cache else load_ticket_cache()
        # Completed events are historical and reused from cache (fast). Live
        # events are still selling/checking-in tickets, so they're always
        # fetched fresh. This is the main speed-up on repeat runs.
        to_fetch = []
        for row in candidates:
            cached = cache.get(row["id"])
            if row["status"] == "completed" and cached and not args.refresh_cache:
                row.update(cached)
                rows.append(row)
            else:
                to_fetch.append(row)

        total = len(to_fetch)
        done = 0
        lock = threading.Lock()
        start_time = time.time()

        def worker(row):
            stats = get_event_ticket_stats(row["id"], token)
            return row, stats

        if total:
            print(f"{len(rows)} event(s) reused from cache; fetching ticket stats for "
                  f"{total} event(s) with {args.workers} parallel workers...", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(worker, row) for row in to_fetch]
            for future in as_completed(futures):
                row, stats = future.result()
                error = stats.pop("error", None)
                with lock:
                    done += 1
                    if error:
                        ticket_errors.append(row["name"])
                    row.update(stats)
                    rows.append(row)
                    if row["status"] == "completed" and not error:
                        cache[row["id"]] = stats
                    elapsed = time.time() - start_time
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    sys.stderr.write(
                        f"\rFetching ticket stats {done}/{total} "
                        f"({rate:.1f}/s, ~{int(eta)}s left)      "
                    )
                    sys.stderr.flush()
                    if done % 20 == 0:
                        save_ticket_cache(cache)
        if total:
            sys.stderr.write("\n")
        save_ticket_cache(cache)
        rows.sort(key=lambda r: r["start"], reverse=True)
    else:
        for row in candidates:
            row.update({"tickets_total": None, "orders_total": None, "canceled": None, "checked_in": None})
            rows.append(row)

    try:
        # Show Pacific time (auto-adjusts for PST/PDT) with an explicit
        # abbreviation, instead of the runner's naive local clock (which is
        # UTC on GitHub Actions and reads as confusing/"wrong" to a
        # Pacific-based team without a label).
        from zoneinfo import ZoneInfo
        generated_at = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = build_dashboard_html(rows, org_name, generated_at, excluded)

    out_path = args.out or "eventbrite_dashboard.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard written to {out_path} ({len(rows)} events: "
          f"{sum(1 for r in rows if r['status'] == 'live')} live, "
          f"{sum(1 for r in rows if r['status'] == 'completed')} completed).")
    if args.tickets:
        total_tickets = sum(r["tickets_total"] or 0 for r in rows)
        total_orders = sum(r["orders_total"] or 0 for r in rows)
        total_canceled = sum(r["canceled"] or 0 for r in rows)
        total_checked_in = sum(r["checked_in"] or 0 for r in rows)
        print(f"Tickets: {total_tickets}  Orders: {total_orders}  "
              f"Canceled: {total_canceled}  Checked in: {total_checked_in}")
        if ticket_errors:
            print(f"Note: couldn't read attendee data for {len(ticket_errors)} event(s) "
                  f"(shown as blank in the dashboard): {', '.join(ticket_errors[:5])}"
                  + (", ..." if len(ticket_errors) > 5 else ""))
    print("Open it by double-clicking the file, or run: open " + out_path)


TEMPLATE_FILENAME = "dashboard_template.html"
LOGO_FILENAME = "logo.png"


def load_template():
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), TEMPLATE_FILENAME)
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        sys.stderr.write(
            f"ERROR: couldn't read {TEMPLATE_FILENAME} next to eventbrite_tool.py ({e}).\n"
            "Make sure dashboard_template.html was copied/committed alongside the script.\n"
        )
        sys.exit(1)


def load_logo_data_uri():
    """Embed logo.png (if present next to the script) as a base64 data URI.

    Returns "" if there's no logo file, so the dashboard just hides the logo
    badge instead of showing a broken image.
    """
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOGO_FILENAME)
    try:
        with open(logo_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except OSError:
        return ""


def build_dashboard_html(rows, org_name, generated_at, excluded_count):
    excluded_note = ""
    if excluded_count:
        excluded_note = f"({excluded_count} draft/canceled/other-status events from 2025-2026 were excluded.)"
    html = load_template()
    html = html.replace("__ORG_NAME__", org_name.replace("<", "&lt;").replace(">", "&gt;"))
    html = html.replace("__GENERATED_AT__", generated_at)
    html = html.replace("__EXCLUDED_NOTE__", excluded_note)
    html = html.replace("__LOGO_DATA_URI__", load_logo_data_uri())
    html = html.replace("__EVENTS_JSON__", json.dumps(rows))
    return html


def main():
    parser = argparse.ArgumentParser(description="Eventbrite API helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("orgs", help="List your Eventbrite organizations")

    p_events = sub.add_parser("events", help="List events for an organization")
    p_events.add_argument("--org", help="Organization ID (defaults to first org)")
    p_events.add_argument("--status", help="Filter by status (all, live, draft, started, ended, completed, canceled)")

    p_attendees = sub.add_parser("attendees", help="List attendees for an event")
    p_attendees.add_argument("--event", required=True, help="Event ID")

    p_orders = sub.add_parser("orders", help="List orders for an event")
    p_orders.add_argument("--event", required=True, help="Event ID")

    p_dashboard = sub.add_parser("dashboard", help="Build a local HTML dashboard of 2025-2026 live/completed events")
    p_dashboard.add_argument("--org", help="Organization ID (defaults to first org)")
    p_dashboard.add_argument("--out", help="Output HTML file path (default: eventbrite_dashboard.html)")
    p_dashboard.add_argument("--no-tickets", dest="tickets", action="store_false",
                              help="Skip per-event ticket/order/check-in stats (faster, fewer API calls)")
    p_dashboard.add_argument("--workers", type=int, default=8,
                              help="Parallel workers for fetching ticket stats (default: 8)")
    p_dashboard.add_argument("--refresh-cache", action="store_true",
                              help="Ignore the local ticket-stats cache and re-fetch every event")
    p_dashboard.set_defaults(tickets=True)

    args = parser.parse_args()
    token = get_token()

    if args.command == "orgs":
        cmd_orgs(args, token)
    elif args.command == "events":
        cmd_events(args, token)
    elif args.command == "attendees":
        cmd_attendees(args, token)
    elif args.command == "orders":
        cmd_orders(args, token)
    elif args.command == "dashboard":
        cmd_dashboard(args, token)


if __name__ == "__main__":
    main()
