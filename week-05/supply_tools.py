"""Brookfield Bicycle Company's supply data, and the three tools the supply analyst uses.

Plain Python with no AI in it, so the numbers can be checked on their own:

    get_part(part_id)                  stock on hand, scheduled shipments, models that use it
    get_orders_for_part(part_id)       open orders that need the part
    check_coverage(part_id, arrivals)  each order covered, or units short and the first
                                       day it can ship, if arrivals replace the part's
                                       scheduled shipments

All days are counted from today (day 0). Orders are filled complete in ship-day
order, so a short order holds up the ones after it.

The data is read from week-05/brookfield-bikes/*.csv on every call, so an edit
to a CSV file is picked up without restarting anything.

Run this file to print the tools' results for the three sample emails:
    .venv\\Scripts\\python.exe week-05\\supply_tools.py
"""

import csv
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "brookfield-bikes"


class SupplyError(ValueError):
    """A problem with a tool's input, worded for Claude to read and fix."""


def read_csv(name):
    with (DATA_DIR / name).open(encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def find_part(part_id):
    part_id = (part_id or "").strip().upper()
    for row in read_csv("parts.csv"):
        if row["part_id"].upper() == part_id:
            return row
    known = ", ".join(r["part_id"] for r in read_csv("parts.csv"))
    raise SupplyError(f"No part {part_id!r}. Known parts: {known}.")


def inbound_for(part_id):
    return [
        {"po_number": r["po_number"], "supplier": r["supplier"],
         "qty": int(r["qty"]), "arrives_in_days": int(r["arrives_in_days"])}
        for r in read_csv("inbound.csv") if r["part_id"] == part_id
    ]


def get_part(part_id):
    """The part's description, stock on hand, supplier and scheduled shipments."""
    part = find_part(part_id)
    pid = part["part_id"]
    return {
        "part_id": pid,
        "description": part["description"],
        "supplier": part["supplier"],
        "on_hand": int(part["on_hand"]),
        "scheduled_shipments": inbound_for(pid),
        "used_in_models": sorted({r["model"] for r in read_csv("bill-of-materials.csv")
                                  if r["part_id"] == pid}),
    }


def open_orders(part_id):
    """Open orders needing the part, earliest ship date first, with the parts each needs."""
    per_bike = {r["model"]: int(r["qty_per_bike"])
                for r in read_csv("bill-of-materials.csv") if r["part_id"] == part_id}
    orders = []
    for r in read_csv("orders.csv"):
        if r["model"] in per_bike:
            bikes = int(r["qty"])
            orders.append({
                "order_id": r["order_id"],
                "customer": r["customer"],
                "model": r["model"],
                "bikes": bikes,
                "parts_per_bike": per_bike[r["model"]],
                "units_needed": bikes * per_bike[r["model"]],
                "ship_day": int(r["ship_in_days"]),
                "late_penalty": r["late_penalty"].lower() == "yes",
            })
    orders.sort(key=lambda o: (o["ship_day"], o["order_id"]))
    return orders


def get_orders_for_part(part_id):
    """Open orders that need the part, earliest ship date first."""
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
        row = {"order_id": o["order_id"], "customer": o["customer"], "model": o["model"],
               "bikes": o["bikes"], "units_needed": o["units_needed"],
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


if __name__ == "__main__":
    for title, args in [
        ("Current schedule, MC-500 (400 on day 5)",
         ("MC-500", [{"qty": 400, "arrives_in_days": 5}])),
        ("Northgate: 120 on day 5, 280 three weeks later (day 26)",
         ("MC-500", [{"qty": 120, "arrives_in_days": 5}, {"qty": 280, "arrives_in_days": 26}])),
        ("Kendall option 1: 160 on day 8, 240 two weeks later (day 22)",
         ("TR-29", [{"qty": 160, "arrives_in_days": 8}, {"qty": 240, "arrives_in_days": 22}])),
        ("Kendall option 2: all 400 four days late (day 12)",
         ("TR-29", [{"qty": 400, "arrives_in_days": 12}])),
        ("Keystone: on schedule (150 on day 6)",
         ("BP-48", [{"qty": 150, "arrives_in_days": 6}])),
    ]:
        print(f"== {title}")
        for r in check_coverage(*args)["orders"]:
            if r["status"] == "covered":
                print(f"  {r['order_id']}  ship day {r['ship_day']:>2}  covered")
            else:
                print(f"  {r['order_id']}  ship day {r['ship_day']:>2}  SHORT {r['units_short']} units,"
                      f" can ship day {r['ready_day']} ({r['days_late']} days late),"
                      f" penalty {'yes' if r['late_penalty'] else 'no'}")
