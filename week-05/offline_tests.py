"""Offline tests for the week-05 supply agents. No Claude API calls, no API key.

Checks, for brookfield-bikes and copperline-coffee:
  - CSV columns are found by their headers (and a made-up organization with other names)
  - order coverage arithmetic, against a separate day-by-day simulation and known answers
  - customer-name privacy on the supplier side, and the name checks on drafts
  - the supplier's impact (orders delayed, fewest/most days late) computed in code, and the
    check that marks a supplier draft whose delay range conflicts with it
  - prompts and drafts folders follow --org
  - the MCP server starts with --org and its tools return the same numbers
  - the FINAL DRAFT printout
  - the source data files are not changed by the tests

From the top of the repository:
    .venv\\Scripts\\python.exe week-05\\offline_tests.py
"""

import asyncio
import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import specialists  # noqa: E402
import supply_tools  # noqa: E402

ORGS = ("brookfield-bikes", "copperline-coffee")
PASSED, FAILED = [], []


def check(condition, what):
    (PASSED if condition else FAILED).append(what)
    print(f"  {'ok  ' if condition else 'FAIL'}  {what}")


def no_api(*_args, **_kwargs):
    raise AssertionError("An offline test tried to use the Claude API or the API key.")


# The tests must never reach Claude or read the key.
specialists.load_api_key = no_api
specialists.client = no_api
specialists.async_client = no_api


def data_hashes():
    files = [p for org in ORGS for p in sorted((HERE / org).rglob("*")) if p.is_file()]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


# ---------------------------------------------------------------------------

def test_headers():
    print("\nColumns found by header")
    expected = {
        "brookfield-bikes": {"product": "model", "quantity": "qty", "ship_day": "ship_in_days",
                             "per_unit": "qty_per_bike"},
        "copperline-coffee": {"product": "product", "quantity": "units", "ship_day": "ship_by_day",
                              "per_unit": "per_unit"},
    }
    for org in ORGS:
        folder = supply_tools.org_folder(org)
        for name in supply_tools.CSV_FILES:
            with (folder / name).open(encoding="utf-8-sig") as f:
                headers = [h.strip() for h in f.readline().split(",")]
            cols = supply_tools.find_columns(headers, supply_tools.FILE_ROLES[name], name)
            if name == "orders.csv":
                for role in ("product", "quantity", "ship_day"):
                    check(cols[role] == expected[org][role], f"{org} orders.csv: {role} = {cols[role]}")
            if name == "bill-of-materials.csv":
                check(cols["per_unit"] == expected[org]["per_unit"],
                      f"{org} bill-of-materials.csv: parts per unit = {cols['per_unit']}")
            if name not in ("orders.csv", "bill-of-materials.csv"):
                check(len(cols) == len(supply_tools.FILE_ROLES[name]), f"{org} {name}: all columns found")


def test_made_up_org():
    print("\nA made-up organization with its own header names and column order")
    files = {
        "parts.csv": "Part Number,Stock On Hand,Vendor,Description\nX-1,10,Acme Parts,Widget\n",
        "inbound.csv": "Arrival Day,Quantity,PO,Part Number,Vendor\n3,20,PO-1,X-1,Acme Parts\n",
        "bill-of-materials.csv": "Units Per Item,Part No,Item\n2,X-1,Gadget\n",
        "orders.csv": ("Order Number,Customer Name,Contact Email,Item,Order Quantity,Ship Day,Penalty If Late\n"
                       "A-2,Zed Co,z@z.example,Gadget,4,5,no\n"
                       "A-1,Yew Ltd,y@y.example,Gadget,5,2,yes\n"),
        "suppliers.csv": "Vendor,Contact Name,Contact Email,Part Number\nAcme Parts,Ann Lee,a@acme.example,X-1\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8")
        old = supply_tools.DATA_DIR
        supply_tools.DATA_DIR = folder
        try:
            result = supply_tools.check_coverage("x-1", [{"qty": 20, "arrives_in_days": 3}])
            rows = {r["order_id"]: r for r in result["orders"]}
            # A-1 needs 10 by day 2 with 10 on hand: covered. A-2 needs 8 more by day 5: 20 arrive day 3.
            check(rows["A-1"]["status"] == "covered" and rows["A-2"]["status"] == "covered",
                  "made-up org: both orders covered (10 on hand, 20 on day 3)")
            result = supply_tools.check_coverage("X-1", [{"qty": 20, "arrives_in_days": 9}])
            rows = {r["order_id"]: r for r in result["orders"]}
            check(rows["A-2"]["units_short"] == 8 and rows["A-2"]["ready_day"] == 9,
                  "made-up org: A-2 is 8 short and ready on day 9 when the shipment slips")
            check(rows["A-1"]["late_penalty"] is True, "made-up org: 'Penalty If Late' read as a late penalty")
            (folder / "orders.csv").write_text("Order Number,Customer Name,Item,Ship Day,Penalty\nA-1,Y,G,2,no\n",
                                               encoding="utf-8")
            try:
                supply_tools.read_orders()
                check(False, "a missing quantity column is reported")
            except supply_tools.SupplyError as exc:
                check("quantity" in str(exc), "a missing quantity column is reported: " + str(exc)[:70])
        finally:
            supply_tools.DATA_DIR = old


def simulate(on_hand, arrivals, orders):
    """A separate day-by-day simulation: {order_id: (units_short, ready_day)}."""
    orders = sorted(orders, key=lambda o: (o["ship_day"], o["order_id"]))
    last = max([a["arrives_in_days"] for a in arrivals] + [o["ship_day"] for o in orders]) + 1
    by_day = {}
    for a in arrivals:
        by_day[a["arrives_in_days"]] = by_day.get(a["arrives_in_days"], 0) + a["qty"]
    # Ready day: ship in order, each as soon as stock covers it.
    stock, queue, ready = on_hand, list(orders), {}
    for day in range(0, last + 1):
        stock += by_day.get(day, 0)
        while queue and stock >= queue[0]["units_needed"]:
            stock -= queue[0]["units_needed"]
            ready[queue.pop(0)["order_id"]] = day
    for o in queue:
        ready[o["order_id"]] = None
    # Units short on the ship day: what is left after every earlier order takes its share.
    out, earlier = {}, 0
    for o in orders:
        supply = on_hand + sum(q for d, q in by_day.items() if d <= o["ship_day"])
        short = min(o["units_needed"], max(0, earlier + o["units_needed"] - supply))
        out[o["order_id"]] = (short, ready[o["order_id"]] if short else None)
        earlier += o["units_needed"]
    return out


def test_coverage():
    print("\nOrder coverage arithmetic")
    for org in ORGS:
        supply_tools.set_org(org)
        for title, part_id, arrivals in supply_tools.SCENARIOS[org]:
            result = supply_tools.check_coverage(part_id, arrivals)
            part = supply_tools.get_part(part_id)
            expected = simulate(part["on_hand"], arrivals, supply_tools.open_orders(part_id))
            got = {r["order_id"]: (r.get("units_short", 0), r.get("ready_day")) for r in result["orders"]}
            check(got == expected, f"{org}: {title}")

    known = {
        ("brookfield-bikes", "MC-500"): ([{"qty": 120, "arrives_in_days": 5}, {"qty": 280, "arrives_in_days": 26}],
                                        {"O-1043": (10, 26, 12), "O-1044": (30, 26, 8), "O-1045": (50, 26, 5)}),
        ("copperline-coffee", "PU-20"): ([{"qty": 40, "arrives_in_days": 4}, {"qty": 160, "arrives_in_days": 19}],
                                        {"O-2203": (10, 19, 7), "O-2204": (20, 19, 4), "O-2205": (25, 19, 2)}),
        ("copperline-coffee", "BL-12"): ([{"qty": 200, "arrives_in_days": 13}], {"O-2203": (40, 13, 1)}),
    }
    for (org, part_id), (arrivals, short) in known.items():
        supply_tools.set_org(org)
        result = supply_tools.check_coverage(part_id, arrivals)
        got = {r["order_id"]: (r["units_short"], r["ready_day"], r["days_late"])
               for r in result["orders"] if r["status"] == "short"}
        check(got == short, f"{org} {part_id}: known answer {short}")

    supply_tools.set_org("copperline-coffee")
    needs = {o["order_id"]: o["units_needed"] for o in supply_tools.open_orders("PU-20")}
    check(needs["O-2202"] == 30 and needs["O-2201"] == 20,
          "copperline: Cafe Duo needs 2 pumps per machine (15 x 2 = 30), Cafe One 1 (20 x 1 = 20)")


def test_privacy():
    print("\nCustomer-name privacy")
    for org in ORGS:
        specialists.set_org(org)
        customers = specialists.customer_names()
        suppliers = specialists.supplier_names()
        orders = supply_tools.read_orders()
        brief = {
            "part_id": "P-1",
            "what_changed": f"Late, hitting {customers[0]} and {customers[1].upper()}.",
            "orders_at_risk": [{"order_id": o["order_id"], "customer": o["customer"], "units_short": 1,
                                "ship_day": o["ship_day"], "ready_day": o["ship_day"] + 2,
                                "late_penalty": o["late_penalty"]} for o in orders],
            "options": [],
            "decisions_for_a_person": [f"Call {customers[-1]} about the penalty."],
            "supplier_questions": [f"Is {customers[2]} affected?"],
        }
        email = f"Please send each customer's name, such as {customers[3]}."
        clean_email, clean_brief = specialists.for_supplier(email, brief)
        text = clean_email + json.dumps(clean_brief)
        check(not specialists.names_in(text, customers), f"{org}: no customer name reaches the supplier writer")
        check("late_penalty" not in text and "decisions_for_a_person" not in text
              and "penalty" not in text.replace(specialists.IMPACT_NOTE, ""),
              f"{org}: no late penalties or internal decisions reach the supplier writer")
        check(not specialists.names_in(text, [o["order_id"] for o in orders]),
              f"{org}: no order ID reaches the supplier writer")
        check(set(clean_brief) == {"part_id", "what_changed", "impact", "impact_note", "options",
                                   "supplier_questions"} and "orders_at_risk" not in text
              and "ship_day" not in text and "ready_day" not in text,
              f"{org}: the supplier brief has only the allowed fields, no per-order details")
        check(brief["orders_at_risk"][0]["customer"] == orders[0]["customer"],
              f"{org}: the original brief is left unchanged")

        verdict = {"verdict": "APPROVE"}
        bad = specialists.supplier_reasons(f"Hello, {customers[4]} is waiting.", verdict, 0, brief)
        check(any("Names a customer" in r for r in bad), f"{org}: a supplier draft naming a customer is marked")
        clean = specialists.supplier_reasons("Hello, three orders are late.", verdict, 0,
                                             {"orders_at_risk": []})
        check(clean == [], f"{org}: a clean supplier draft is not marked")

        facts, _ = specialists.order_facts({"order_id": orders[0]["order_id"], "ready_day": 99})
        others = [c for c in customers if c != orders[0]["customer"]]
        check(not specialists.names_in(json.dumps(facts), others + suppliers),
              f"{org}: a customer notice's facts hold only that customer's name")
        check(not any("day" in k and k != "days_late" for k in facts),
              f"{org}: no internal day numbers in a customer notice's facts")
        bad = specialists.customer_reasons(f"Delayed by {suppliers[0]}; {others[0]} too.", verdict, 0,
                                           facts, False, None, False)
        check(any("supplier" in r for r in bad) and any("another customer" in r for r in bad),
              f"{org}: a customer draft naming a supplier or another customer is marked")


def brief_from_coverage(org, part_id, schedules):
    """The brief a correct analyst would write, built from check_coverage (one schedule, or
    one per option)."""
    supply_tools.set_org(org)
    at_risk, options = {}, []
    for name, arrivals in schedules:
        short = [r for r in supply_tools.check_coverage(part_id, arrivals)["orders"] if r["status"] == "short"]
        for r in short:
            at_risk.setdefault(r["order_id"], {k: r[k] for k in ("order_id", "customer", "units_short",
                                                                  "ship_day", "ready_day", "late_penalty")})
        options.append({"name": name, "order_ids": [r["order_id"] for r in short],
                        "days_late": [r["days_late"] for r in short]})
    brief = {"part_id": part_id, "what_changed": "", "orders_at_risk": list(at_risk.values()),
             "options": [{"name": o["name"], "order_ids": o["order_ids"]} for o in options] if len(schedules) > 1 else [],
             "decisions_for_a_person": [], "supplier_questions": []}
    return brief, options


def test_impact():
    print("\nSupplier impact computed in code, and the delay-range check")
    cases = [("brookfield-bikes", "MC-500", [("split", supply_tools.SCENARIOS["brookfield-bikes"][1][2])]),
             ("brookfield-bikes", "TR-29", [("Option 1", supply_tools.SCENARIOS["brookfield-bikes"][2][2]),
                                            ("Option 2", supply_tools.SCENARIOS["brookfield-bikes"][3][2])]),
             ("copperline-coffee", "PU-20", [("split", supply_tools.SCENARIOS["copperline-coffee"][1][2])]),
             ("copperline-coffee", "BL-12", [("Option A", supply_tools.SCENARIOS["copperline-coffee"][2][2]),
                                             ("Option B", supply_tools.SCENARIOS["copperline-coffee"][3][2])])]
    for org, part_id, schedules in cases:
        brief, options = brief_from_coverage(org, part_id, schedules)
        specialists.set_org(org)
        impact, by_option, problems = specialists.delay_impact(brief)
        check(problems == [], f"{org} {part_id}: the analyst's orders check out against orders.csv")
        # Expected values come from check_coverage's own days_late, not from the brief.
        for o, got in zip(options, by_option if len(schedules) > 1 else [impact]):
            want = {"orders_delayed": len(o["order_ids"])}
            if o["days_late"]:
                want.update(days_late_fewest=min(o["days_late"]), days_late_most=max(o["days_late"]))
            got = {k: v for k, v in got.items() if k != "name"}
            check(got == want, f"{org} {part_id} {o['name']}: {want}")
        # With options only each option's own range is right; otherwise the overall range.
        groups = by_option if brief["options"] else [impact]
        for g in [g for g in groups if "days_late_most" in g]:
            lo, hi = g["days_late_fewest"], g["days_late_most"]
            check(specialists.wrong_day_ranges(f"delayed by {lo} to {hi} days", groups) == [],
                  f"{org} {part_id}: the correct range '{lo} to {hi} days' passes")
            check(specialists.wrong_day_ranges(f"delayed by up to {hi} days", groups) == [],
                  f"{org} {part_id}: 'up to {hi} days' passes")
            check(specialists.wrong_day_ranges(f"delayed {lo + 1}-{hi + 3} days", groups) != [],
                  f"{org} {part_id}: a wrong range '{lo + 1}-{hi + 3} days' is flagged")
            reasons = specialists.supplier_reasons(f"Orders will slip {lo + 2} to {hi + 2} days.",
                                                   {"verdict": "APPROVE"}, 0, brief)
            check(any("conflicts with the computed facts" in r for r in reasons),
                  f"{org} {part_id}: a supplier draft with a wrong range is marked for a person")
        _, clean = specialists.for_supplier("", brief)
        if brief["options"]:
            check(isinstance(clean["impact"], str) and all("orders_delayed" in o for o in clean["options"]),
                  f"{org} {part_id}: with options, the supplier brief has only each option's impact")
            ranged = [g for g in by_option if "days_late_most" in g]
            if len(ranged) > 1:
                lo = min(g["days_late_fewest"] for g in ranged)
                hi = max(g["days_late_most"] for g in ranged)
                reasons = specialists.supplier_reasons(f"This affects orders by {lo} to {hi} days.",
                                                       {"verdict": "APPROVE"}, 0, brief)
                check(any("conflicts with the computed facts" in r for r in reasons),
                      f"{org} {part_id}: a combined range across options '{lo} to {hi} days' is flagged")
        else:
            check(clean["impact"] == impact, f"{org} {part_id}: without options, the overall impact is given")

    specialists.set_org("copperline-coffee")
    bad = {"orders_at_risk": [{"order_id": "O-9999", "ship_day": 1, "ready_day": 5},
                              {"order_id": "O-2203", "ship_day": 3, "ready_day": 19}]}
    _, _, problems = specialists.delay_impact(bad)
    check(len(problems) == 2, f"an unknown order and a wrong ship day are reported: {problems}")
    reasons = specialists.supplier_reasons("Thanks.", {"verdict": "APPROVE"}, 0, bad)
    check(any("didn't check out" in r for r in reasons), "a supplier draft from unchecked data is marked")


def test_reconcile():
    print("\nBrief reconciled against every check_coverage result, for every option")
    sc = supply_tools.SCENARIOS
    cases = [("copperline-coffee", "BL-12", sc["copperline-coffee"][2], sc["copperline-coffee"][3]),
             ("brookfield-bikes", "TR-29", sc["brookfield-bikes"][2], sc["brookfield-bikes"][3])]
    for org, part_id, (title_a, _, arr_a), (title_b, _, arr_b) in cases:
        specialists.set_org(org)
        checks = [supply_tools.check_coverage(part_id, arr_a), supply_tools.check_coverage(part_id, arr_b)]
        short_a = {r["order_id"]: r for r in checks[0]["orders"] if r["status"] == "short"}
        short_b = {r["order_id"]: r for r in checks[1]["orders"] if r["status"] == "short"}
        # The analyst's slip: options right, but orders_at_risk lists only option B's orders.
        brief = {"part_id": part_id, "what_changed": "",
                 "orders_at_risk": [{k: r[k] for k in ("order_id", "customer", "units_short", "ship_day",
                                                       "ready_day", "late_penalty")} for r in short_b.values()],
                 "options": [{"name": "Option A", "order_ids": sorted(short_a)},
                             {"name": "Option B", "order_ids": sorted(short_b)}],
                 "decisions_for_a_person": ["choose"], "supplier_questions": []}
        rec = specialists.reconcile_brief(brief, checks)
        ids = {o["order_id"] for o in rec["orders_at_risk"]}
        check(ids == set(short_a) | set(short_b),
              f"{org}: orders at risk under every option are listed: {sorted(ids)}")
        check(rec["reconciliation"]["problems"] == [], f"{org}: no problems")
        check(bool(rec["reconciliation"]["corrections"]) == bool(set(short_a) - set(short_b)),
              f"{org}: the correction is recorded: {rec['reconciliation']['corrections']}")
        for oid in ids:
            entry = next(o for o in rec["orders_at_risk"] if o["order_id"] == oid)
            want = {name: s[oid]["days_late"] for name, s in (("Option A", short_a), ("Option B", short_b))
                    if oid in s}
            got = {name: v["days_late"] for name, v in entry["by_option"].items()}
            check(got == want, f"{org} {oid}: option-specific days late kept {got}")
            check(specialists.delay_is_conditional(rec, oid), f"{org} {oid}: its notice is conditional")
            facts, _ = specialists.order_facts(entry, True)
            check(facts["delay_is_certain"] is False and facts["days_late"] == max(want.values()),
                  f"{org} {oid}: notice facts say 'not certain', up to {max(want.values())} days")
        impact, by_option, problems = specialists.delay_impact(rec)
        for o, s in zip(by_option, (short_a, short_b)):
            late = [r["days_late"] for r in s.values()]
            want = {"name": o["name"], "orders_delayed": len(late)}
            if late:
                want.update(days_late_fewest=min(late), days_late_most=max(late))
            check(o == want, f"{org}: supplier impact for {o['name']}: {want}")
        _, clean = specialists.for_supplier("", rec)
        check(not specialists.names_in(json.dumps(clean), list(ids)) and "by_option" not in json.dumps(clean),
              f"{org}: no order IDs or per-order impacts reach the supplier writer")

    specialists.set_org("copperline-coffee")
    split = supply_tools.check_coverage("PU-20", sc["copperline-coffee"][1][2])
    good = {"part_id": "PU-20", "options": [], "orders_at_risk": [
        {k: r[k] for k in ("order_id", "customer", "units_short", "ship_day", "ready_day", "late_penalty")}
        for r in split["orders"] if r["status"] == "short"]}
    rec = specialists.reconcile_brief(good, [supply_tools.check_coverage("PU-20", sc["copperline-coffee"][0][2]), split])
    check(rec["orders_at_risk"] == good["orders_at_risk"] and not rec["reconciliation"]["corrections"]
          and not rec["reconciliation"]["problems"], "a correct brief with no options is left as it is")
    wrong = copy.deepcopy(good)
    wrong["orders_at_risk"][0]["units_short"] += 5
    wrong["orders_at_risk"] = wrong["orders_at_risk"][:2]
    rec = specialists.reconcile_brief(wrong, [split])
    check(rec["orders_at_risk"] == good["orders_at_risk"] and len(rec["reconciliation"]["corrections"]) == 3,
          f"wrong numbers and a missing order are corrected from check_coverage: {rec['reconciliation']['corrections']}")
    made_up = {"part_id": "PU-20", "options": [], "orders_at_risk": [
        {"order_id": "O-2210", "customer": "x", "units_short": 3, "ship_day": 33, "ready_day": 40, "late_penalty": False}]}
    rec = specialists.reconcile_brief(made_up, [split])
    check(rec["orders_at_risk"] == [] and rec["reconciliation"]["problems"],
          f"an order no tool result shows short is dropped and reported: {rec['reconciliation']['problems']}")
    reasons = specialists.customer_reasons("Hi", {"verdict": "APPROVE"}, 0, {"customer": "x"}, False, rec, False)
    check(any("didn't fully check out" in r for r in reasons), "drafts from that brief are marked for a person")
    none = specialists.reconcile_brief({"part_id": "SW-3", "options": [], "orders_at_risk": []}, [])
    check(none["orders_at_risk"] == [] and not none["reconciliation"]["problems"],
          "no change and no tool calls: nothing at risk, no problems")
    late = specialists.reconcile_brief({"part_id": "BR-64", "options": [], "orders_at_risk": []},
                                       [supply_tools.check_coverage("BR-64", sc["copperline-coffee"][4][2])])
    check(late["orders_at_risk"] == [] and not late["reconciliation"]["problems"],
          "a late shipment that delays no order: nothing at risk, no problems")

    print("\nSupplier replies never choose an option")
    opts = {"options": [{"name": "Option A", "order_ids": []}, {"name": "Option B", "order_ids": []}],
            "orders_at_risk": []}
    for text, chooses in [("We would like to go with Option A.", True), ("Please proceed with Option B.", True),
                          ("We accept Option B.", True), ("Option A works for us.", True),
                          ("We will confirm our choice of option within three business days.", False),
                          ("Option A delays one order by 4 days; Option B delays one order by 1 day.", False)]:
        reasons = specialists.supplier_reasons(text, {"verdict": "APPROVE"}, 0, opts)
        flagged = any("May choose an option" in r for r in reasons)
        check(flagged == chooses, f"{'flagged' if chooses else 'not flagged'}: {text}")
    check(any("a person must choose" in r for r in specialists.supplier_reasons(
        "We will confirm.", {"verdict": "APPROVE"}, 0, opts)), "every supplier reply about options is marked")


def test_orchestrator_finish():
    print("\nThe orchestrator's finish call is checked before it is accepted")
    import orchestrator
    leaked = "No customer notices were needed." + "</" + "parameter>\n<" + 'parameter name="decision_needed">false'
    cases = [({"summary": leaked, "decision_needed": False}, False, "a summary with leaked tool markup"),
             ({"summary": "   ", "decision_needed": True}, False, "an empty summary"),
             ({"summary": "Fine.", "decision_needed": "false"}, False, "decision_needed as text"),
             ({"summary": "Nothing needs a decision.", "decision_needed": False}, True, "a clean summary")]
    for args, accepted, what in cases:
        job = orchestrator.Job("email")
        text, is_error = job.run_tool("finish", args)
        check((job.finished is not None) == accepted and is_error != accepted,
              f"{what} is {'accepted' if accepted else 'rejected'}")
    job = orchestrator.Job("email")
    job.run_tool("finish", cases[0][0])
    job.run_tool("finish", cases[3][0])
    check(job.finished == ("Nothing needs a decision.", False), "after a rejection, a clean retry is accepted")


def test_folders():
    print("\nPrompts and drafts folders follow --org")
    for org in ORGS:
        specialists.set_org(org)
        company = "Brookfield" if org == "brookfield-bikes" else "Copperline"
        prompts_ok = all(company in specialists.system_prompt(p) for p in
                         ("analyst", "orchestrator", "reviewer", "supplier-writer", "customer-writer"))
        check(prompts_ok, f"{org}: all five prompts come from {org}/prompts")
        check(specialists.DRAFTS_DIR == HERE / "drafts" / org, f"{org}: drafts go to week-05/drafts/{org}")
    try:
        specialists.set_org("no-such-org")
        check(False, "an unknown --org is refused")
    except specialists.SpecialistError:
        check(True, "an unknown --org is refused")


async def call_server(org, calls):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    server = StdioServerParameters(command=sys.executable,
                                   args=[str(HERE / "supply_mcp_server.py"), "--org", org])
    with open(Path(tempfile.gettempdir()) / "offline_tests_server.log", "w", encoding="utf-8") as log:
        async with stdio_client(server, errlog=log) as (read, write):
            async with ClientSession(read, write) as mcp:
                await mcp.initialize()
                names = sorted(t.name for t in (await mcp.list_tools()).tools)
                results = []
                for name, args in calls:
                    r = await mcp.call_tool(name, args)
                    results.append(json.loads("".join(c.text for c in r.content if c.type == "text")))
                return names, results


def test_mcp_server():
    print("\nMCP server with --org")
    cases = {"brookfield-bikes": ("MC-500", [{"qty": 120, "arrives_in_days": 5}, {"qty": 280, "arrives_in_days": 26}]),
             "copperline-coffee": ("PU-20", [{"qty": 40, "arrives_in_days": 4}, {"qty": 160, "arrives_in_days": 19}])}
    for org, (part_id, arrivals) in cases.items():
        names, (part, cov) = asyncio.run(call_server(org, [
            ("get_part", {"part_id": part_id}),
            ("check_coverage", {"part_id": part_id, "arrivals": arrivals})]))
        supply_tools.set_org(org)
        check(names == ["check_coverage", "get_orders_for_part", "get_part"], f"{org}: server lists the three tools")
        check(part == supply_tools.get_part(part_id), f"{org}: get_part over MCP matches supply_tools")
        check(cov == supply_tools.check_coverage(part_id, arrivals), f"{org}: check_coverage over MCP matches")


def test_final_draft_print():
    print("\nFINAL DRAFT printout")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        specialists.print_final("customer", "O-2203", "Dear customer,\nSorry.\n", True)
        specialists.print_final("supplier", None, "Hello,\nThanks.", False)
    lines = out.getvalue().splitlines()
    check(lines == ["", "FINAL DRAFT TO CUSTOMER O-2203", "Dear customer,", "Sorry.", "Marked for a person: yes",
                    "", "FINAL DRAFT TO SUPPLIER", "Hello,", "Thanks.", "Marked for a person: no"],
          "headings, the draft once, and 'Marked for a person: yes/no'")


def main():
    before = data_hashes()
    for test in (test_headers, test_made_up_org, test_coverage, test_privacy, test_impact, test_reconcile,
                 test_orchestrator_finish, test_folders,
                 test_mcp_server, test_final_draft_print):
        try:
            test()
        except Exception as exc:  # report and keep going
            check(False, f"{test.__name__} raised {type(exc).__name__}: {exc}")
    print("\nSource data")
    check(data_hashes() == before, "the organizations' CSV, email and prompt files are unchanged")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
