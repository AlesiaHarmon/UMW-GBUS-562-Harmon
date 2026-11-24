"""An organization's supply data, and the three tools the supply analyst uses.

Plain Python with no AI in it, so the numbers can be checked on their own:

    get_part(part_id)                  stock on hand, scheduled shipments, products that use it
    get_orders_for_part(part_id)       open orders that need the part
    check_coverage(part_id, arrivals)  each order covered, or units short and the first
                                       day it can ship, if arrivals replace the part's
                                       scheduled shipments

All days are counted from today (day 0). Orders are filled complete in ship-day
order, so a short order holds up the ones after it.

Each organization has a folder in week-05 (brookfield-bikes, copperline-coffee, ...)
holding five CSV files: parts.csv, inbound.csv, bill-of-materials.csv, orders.csv and
suppliers.csv. Their columns are found by their headers, not by fixed names, so
"qty_per_bike" and "per_unit" both work as the parts-per-unit column, and "ship_in_days"
and "ship_by_day" both work as the ship day. set_org() picks the organization; the
default is brookfield-bikes. The files are read on every call, so an edit to a CSV file
is picked up without restarting anything.

Run this file to print the tools' results for an organization's sample emails:
    .venv\\Scripts\\python.exe week-05\\supply_tools.py
    .venv\\Scripts\\python.exe week-05\\supply_tools.py --org copperline-coffee
"""

import argparse
import csv
import json
import re
from pathlib import Path

WEEK_DIR = Path(__file__).resolve().parent
DEFAULT_ORG = "brookfield-bikes"
CSV_FILES = ("parts.csv", "inbound.csv", "bill-of-materials.csv", "orders.csv", "suppliers.csv")

DATA_DIR = WEEK_DIR / DEFAULT_ORG  # changed by set_org()


class SupplyError(ValueError):
    """A problem with a tool's input or the data, worded for Claude (or a person) to fix."""


# ---------------------------------------------------------------------------
# Choosing the organization
# ---------------------------------------------------------------------------

def org_folder(name):
    """The organization's folder inside week-05, checked to have what the tools need."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name or ""):
        raise SupplyError(f"--org must be a folder name inside week-05, like {DEFAULT_ORG}.")
    folder = WEEK_DIR / name
    if not folder.is_dir():
        raise SupplyError(f"There is no organization folder week-05/{name}.")
    missing = [f for f in CSV_FILES + ("prompts",) if not (folder / f).exists()]
    if missing:
        raise SupplyError(f"week-05/{name} is missing: {', '.join(missing)}")
    return folder


def set_org(name):
    """Use this organization's data from now on. Returns its folder."""
    global DATA_DIR
    DATA_DIR = org_folder(name)
    return DATA_DIR


# ---------------------------------------------------------------------------
# Finding columns by their headers
# ---------------------------------------------------------------------------

def words(header):
    """A header's lowercase words: "Qty per Bike" and "qty_per_bike" give qty, per, bike."""
    return re.findall(r"[a-z0-9]+", header.lower())


def _any(w, *options):
    return any(o in w for o in options)


# Each column role is recognized by the words in its header. "per" marks a ratio column
# (parts per unit), so it is excluded from the plain quantity roles.
ROLE_TESTS = {
    "part_id": lambda w: "part" in w and "per" not in w and (_any(w, "id", "number", "no") or w == ["part"]),
    "order_id": lambda w: "order" in w and _any(w, "id", "number", "no"),
    "po_number": lambda w: "po" in w or ("purchase" in w and "order" in w),
    "customer": lambda w: _any(w, "customer", "client", "account") and not _any(w, "email", "contact", "id"),
    "product": lambda w: _any(w, "product", "model", "item", "sku", "variant", "machine")
                         and "per" not in w and "id" not in w,
    "quantity": lambda w: _any(w, "qty", "quantity", "units", "count", "amount") and "per" not in w,
    "per_unit": lambda w: "per" in w,
    "ship_day": lambda w: "ship" in w or ("due" in w and _any(w, "day", "days")),
    "arrives_in_days": lambda w: any(x.startswith("arriv") for x in w) or "eta" in w,
    "late_penalty": lambda w: any(x.startswith("penalt") for x in w),
    "on_hand": lambda w: ("on" in w and "hand" in w) or _any(w, "stock", "inventory"),
    "description": lambda w: _any(w, "description", "desc"),
    "supplier": lambda w: _any(w, "supplier", "vendor") and not _any(w, "email", "contact", "id"),
    "contact_name": lambda w: "name" in w and not _any(w, "supplier", "vendor", "customer"),
    "contact_email": lambda w: _any(w, "email", "mail"),
}

# The roles each file must have.
FILE_ROLES = {
    "parts.csv": ("part_id", "description", "on_hand", "supplier"),
    "inbound.csv": ("po_number", "part_id", "supplier", "quantity", "arrives_in_days"),
    "bill-of-materials.csv": ("product", "part_id", "per_unit"),
    "orders.csv": ("order_id", "customer", "product", "quantity", "ship_day", "late_penalty"),
    "suppliers.csv": ("supplier", "contact_name", "contact_email", "part_id"),
}


def find_columns(headers, roles, file_name="the file"):
    """{role: header} for each role, matched by the words in the headers.

    Raises SupplyError if a role matches no header or more than one."""
    found = {}
    for role in roles:
        matches = [h for h in headers if ROLE_TESTS[role](words(h))]
        if len(matches) != 1:
            problem = "no column" if not matches else f"more than one column ({', '.join(matches)})"
            raise SupplyError(f"{file_name}: found {problem} for {role.replace('_', ' ')}. "
                              f"Headers: {', '.join(headers)}")
        found[role] = matches[0]
    return found


def read_table(file_name, data_dir=None):
    """The rows of one CSV file, keyed by role (part_id, quantity, ship_day, ...)."""
    path = (data_dir or DATA_DIR) / file_name
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        columns = find_columns(headers, FILE_ROLES[file_name], file_name)
        rows = []
        for raw in reader:
            row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
            if any(row.values()):
                rows.append({role: row[header] for role, header in columns.items()})
        return rows


def whole_number(value, what):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SupplyError(f"{what} should be a whole number, not {value!r}.") from None


def yes(value):
    return value.strip().lower() in ("yes", "y", "true", "1")


# ---------------------------------------------------------------------------
# The three tools
# ---------------------------------------------------------------------------

def find_part(part_id):
    part_id = (part_id or "").strip().upper()
    parts = read_table("parts.csv")
    for row in parts:
        if row["part_id"].upper() == part_id:
            return row
    known = ", ".join(r["part_id"] for r in parts)
    raise SupplyError(f"No part {part_id!r}. Known parts: {known}.")


def inbound_for(part_id):
    return [
        {"po_number": r["po_number"], "supplier": r["supplier"],
         "qty": whole_number(r["quantity"], "An inbound quantity"),
         "arrives_in_days": whole_number(r["arrives_in_days"], "An arrival day")}
        for r in read_table("inbound.csv") if r["part_id"] == part_id
    ]


def get_part(part_id):
    """The part's description, stock on hand, supplier and scheduled shipments."""
    part = find_part(part_id)
    pid = part["part_id"]
    return {
        "part_id": pid,
        "description": part["description"],
        "supplier": part["supplier"],
        "on_hand": whole_number(part["on_hand"], "Stock on hand"),
        "scheduled_shipments": inbound_for(pid),
        "used_in_products": sorted({r["product"] for r in read_table("bill-of-materials.csv")
                                    if r["part_id"] == pid}),
    }


def read_orders():
    """Every open order: order_id, customer, product, quantity, ship_day, late_penalty."""
    return [{"order_id": r["order_id"], "customer": r["customer"], "product": r["product"],
             "quantity": whole_number(r["quantity"], f"Quantity of {r['order_id']}"),
             "ship_day": whole_number(r["ship_day"], f"Ship day of {r['order_id']}"),
             "late_penalty": yes(r["late_penalty"])}
            for r in read_table("orders.csv")]


def open_orders(part_id):
    """Open orders needing the part, earliest ship day first, with the parts each needs."""
    per_unit = {r["product"]: whole_number(r["per_unit"], "Parts per unit")
                for r in read_table("bill-of-materials.csv") if r["part_id"] == part_id}
    orders = []
    for o in read_orders():
        if o["product"] in per_unit:
            orders.append({
                "order_id": o["order_id"],
                "customer": o["customer"],
                "product": o["product"],
                "quantity": o["quantity"],
                "parts_per_unit": per_unit[o["product"]],
                "units_needed": o["quantity"] * per_unit[o["product"]],
                "ship_day": o["ship_day"],
                "late_penalty": o["late_penalty"],
            })
    orders.sort(key=lambda o: (o["ship_day"], o["order_id"]))
    return orders


def get_orders_for_part(part_id):
    """Open orders that need the part, earliest ship day first."""
    pid = find_part(part_id)["part_id"]
    orders = open_orders(pid)
    return {"part_id": pid, "orders": orders,
            "total_units_needed": sum(o["units_needed"] for o in orders)}


def clean_arrivals(arrivals):
    if not isinstance(arrivals, list) or not arrivals:
        raise SupplyError("arrivals must be a non-empty list of {qty, arrives_in_days}.")
    cleaned = []
    for a in arrivals:
        try:
            qty, day = int(a["qty"]), int(a["arrives_in_days"])
        except (KeyError, TypeError, ValueError):
            raise SupplyError("Each arrival needs a whole-number qty and arrives_in_days.") from None
        if qty <= 0 or day < 0:
            raise SupplyError("Each arrival needs qty above 0 and arrives_in_days of 0 or more.")
        cleaned.append({"qty": qty, "arrives_in_days": day})
    return sorted(cleaned, key=lambda a: a["arrives_in_days"])


def coverage(on_hand, arrivals, orders):
    """Fill orders complete, in ship-day order: a short order holds up the ones after it.

    An order can ship once stock on hand plus arrivals covers it and every order
    before it. That is the first day cumulative supply reaches cumulative demand,
    so an earlier order is never passed over for a later one."""
    events = sorted((a["arrives_in_days"], a["qty"]) for a in arrivals)

    def supply_by(day):
        return on_hand + sum(q for d, q in events if d <= day)

    def first_day_with(units):
        total = on_hand
        if total >= units:
            return 0
        for d, q in events:
            total += q
            if total >= units:
                return d
        return None  # the arrivals never cover it

    results, needed = [], 0
    for o in orders:
        needed += o["units_needed"]
        row = {"order_id": o["order_id"], "customer": o["customer"], "product": o["product"],
               "quantity": o["quantity"], "units_needed": o["units_needed"],
               "ship_day": o["ship_day"], "late_penalty": o["late_penalty"]}
        # Units still missing on the ship day, after every earlier order is filled.
        short = min(o["units_needed"], max(0, needed - supply_by(o["ship_day"])))
        if short == 0:
            row["status"] = "covered"
        else:
            ready = first_day_with(needed)
            row.update(status="short", units_short=short, ready_day=ready,
                       days_late=None if ready is None else ready - o["ship_day"])
        results.append(row)
    return results


def check_coverage(part_id, arrivals):
    """Each open order's coverage if the part's scheduled shipments become `arrivals`.

    arrivals replaces all of the part's scheduled shipments."""
    part = get_part(part_id)
    new = clean_arrivals(arrivals)
    results = coverage(part["on_hand"], new, open_orders(part["part_id"]))
    short = [r for r in results if r["status"] == "short"]
    return {
        "part_id": part["part_id"],
        "on_hand": part["on_hand"],
        "arrivals": new,
        "replaced_shipments": [{"po_number": s["po_number"], "qty": s["qty"],
                                "arrives_in_days": s["arrives_in_days"]}
                               for s in part["scheduled_shipments"]],
        "orders": results,
        "orders_short": [r["order_id"] for r in short],
        "rule": ("Orders are filled complete in ship-day order, so a short order holds up "
                 "the ones after it. ready_day is the first day an order can ship"
                 + ("; null means these arrivals never cover it." if any(
                     r["ready_day"] is None for r in short) else ".")),
    }


# The sample emails' schedules, as arrivals counted from today, for each organization.
SCENARIOS = {
    "brookfield-bikes": [
        ("Current schedule, MC-500 (400 on day 5)",
         "MC-500", [{"qty": 400, "arrives_in_days": 5}]),
        ("Northgate: 120 on day 5, 280 three weeks later (day 26)",
         "MC-500", [{"qty": 120, "arrives_in_days": 5}, {"qty": 280, "arrives_in_days": 26}]),
        ("Kendall option 1: 160 on day 8, 240 two weeks later (day 22)",
         "TR-29", [{"qty": 160, "arrives_in_days": 8}, {"qty": 240, "arrives_in_days": 22}]),
        ("Kendall option 2: all 400 four days late (day 12)",
         "TR-29", [{"qty": 400, "arrives_in_days": 12}]),
        ("Keystone: on schedule (150 on day 6)",
         "BP-48", [{"qty": 150, "arrives_in_days": 6}]),
    ],
    "copperline-coffee": [
        ("Current schedule, PU-20 (200 on day 4)",
         "PU-20", [{"qty": 200, "arrives_in_days": 4}]),
        ("Danforth: 40 on day 4, 160 fifteen days later (day 19)",
         "PU-20", [{"qty": 40, "arrives_in_days": 4}, {"qty": 160, "arrives_in_days": 19}]),
        ("Marlow option A: 80 on day 7, 120 two weeks later (day 21)",
         "BL-12", [{"qty": 80, "arrives_in_days": 7}, {"qty": 120, "arrives_in_days": 21}]),
        ("Marlow option B: all 200 six days late (day 13)",
         "BL-12", [{"qty": 200, "arrives_in_days": 13}]),
        ("Ashgrove: all 120 three days late (day 13)",
         "BR-64", [{"qty": 120, "arrives_in_days": 13}]),
        ("Pelham: on schedule (60 on day 11)",
         "SW-3", [{"qty": 60, "arrives_in_days": 11}]),
    ],
}


def main():
    parser = argparse.ArgumentParser(description="Print the supply tools' results for sample schedules.")
    parser.add_argument("--org", default=DEFAULT_ORG, help=f"organization folder (default {DEFAULT_ORG})")
    args = parser.parse_args()
    set_org(args.org)
    for title, part_id, arrivals in SCENARIOS.get(args.org, []):
        print(f"== {title}")
        for r in check_coverage(part_id, arrivals)["orders"]:
            if r["status"] == "covered":
                print(f"  {r['order_id']}  ship day {r['ship_day']:>2}  covered")
            else:
                print(f"  {r['order_id']}  ship day {r['ship_day']:>2}  SHORT {r['units_short']} units,"
                      f" can ship day {r['ready_day']} ({r['days_late']} days late),"
                      f" penalty {'yes' if r['late_penalty'] else 'no'}")
    if args.org not in SCENARIOS:
        print(json.dumps({p["part_id"]: get_part(p["part_id"]) for p in read_table("parts.csv")}, indent=2))


if __name__ == "__main__":
    main()
