"""An organization's four specialists, shared by the coordinators in week-05.

    run_analyst(email)                 agent with the tools of supply_mcp_server.py; at most
                                       10 tool calls; returns its brief as a dict (JSON)
    write_supplier_reply(email, brief) one model call, given a sanitized brief: no customer
                                       names, order IDs, penalties or internal decisions,
                                       plus the delay's impact computed in code
    write_customer_notice(order)       one model call, given only that one order's facts
    review(draft, facts, recipient)    one model call; returns APPROVE or REVISE with reasons

The review step runs a writer, then the reviewer, and revises at most 2 times:

    reviewed_supplier_reply(email, brief)
    reviewed_customer_notice(order, brief)

set_org(name) picks the organization: a folder in week-05 (default brookfield-bikes) with
its CSV files and its specialists' prompts. Drafts go to week-05/drafts/<org>/. The CSV
columns are found by their headers (see supply_tools.py), so each organization can name
them its own way.

If the supplier offered options and one of them keeps the order on time, the customer
writer is told the delay is possible, not certain. Any customer notice written while the
supplier's options are still open is marked for a person.

After the reviewer, code checks that a supplier draft names no customer and states no
delay range that conflicts with the impact computed in code, and that a customer draft
names no supplier (or other customer). Then the final draft is printed once, after
a FINAL DRAFT TO SUPPLIER or FINAL DRAFT TO CUSTOMER <order ID> line and before a
"Marked for a person: yes/no" line, and saved. A draft is marked for a person if the
reviewer did not approve it, a name check failed, its order has a late penalty, or
(customer notices) the supplier's options are still open.

Each specialist reads its system prompt from <org>/prompts on every run, so an edited
prompt is picked up without restarting anything. Every handoff, the analyst's full brief,
and the tokens each specialist used are printed.

The API key is read from API-Key.txt (or "API Key.txt") in the folder above the
repository. It is only held in memory, never written anywhere.

Try it on one sample email (this calls Claude and costs a few cents):
    .venv\\Scripts\\python.exe week-05\\specialists.py week-05\\brookfield-bikes\\supplier-emails\\01-northgate-controller-delay.eml
    .venv\\Scripts\\python.exe week-05\\specialists.py --org copperline-coffee week-05\\copperline-coffee\\supplier-emails\\01-danforth-pump-delay.eml
"""

import argparse
import asyncio
import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SERVER_SCRIPT = HERE / "supply_mcp_server.py"

sys.path.insert(0, str(HERE))
import supply_tools  # noqa: E402  (found beside this file, whatever the working folder)

DEFAULT_ORG = supply_tools.DEFAULT_ORG

# The key lives next to the repository, never inside it.
KEY_FILES = [REPO_ROOT.parent / "API-Key.txt", REPO_ROOT.parent / "API Key.txt"]

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
MAX_TOOL_CALLS = 10
MAX_REVISIONS = 2
NO_REPLY = "NO REPLY NEEDED"

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "part_id": {"type": "string"},
        "what_changed": {"type": "string"},
        "orders_at_risk": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "customer": {"type": "string"},
                    "units_short": {"type": "integer"},
                    "ship_day": {"type": "integer"},
                    "ready_day": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "late_penalty": {"type": "boolean"},
                },
                "required": ["order_id", "customer", "units_short", "ship_day",
                             "ready_day", "late_penalty"],
                "additionalProperties": False,
            },
        },
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "order_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "order_ids"],
                "additionalProperties": False,
            },
        },
        "decisions_for_a_person": {"type": "array", "items": {"type": "string"}},
        "supplier_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["part_id", "what_changed", "orders_at_risk", "options",
                 "decisions_for_a_person", "supplier_questions"],
    "additionalProperties": False,
}


class SpecialistError(RuntimeError):
    """A specialist could not finish its job."""


# ---------------------------------------------------------------------------
# The organization: data, prompts and drafts folders
# ---------------------------------------------------------------------------

ORG = DEFAULT_ORG
DATA_DIR = HERE / DEFAULT_ORG
PROMPTS_DIR = DATA_DIR / "prompts"
DRAFTS_DIR = HERE / "drafts" / DEFAULT_ORG


def set_org(name):
    """Use this organization's data, prompts and drafts folder from now on."""
    global ORG, DATA_DIR, PROMPTS_DIR, DRAFTS_DIR
    try:
        DATA_DIR = supply_tools.set_org(name)
    except supply_tools.SupplyError as exc:
        raise SpecialistError(str(exc)) from None
    ORG = name
    PROMPTS_DIR = DATA_DIR / "prompts"
    DRAFTS_DIR = HERE / "drafts" / name
    return DATA_DIR


def add_org_argument(parser):
    parser.add_argument("--org", default=DEFAULT_ORG,
                        help=f"organization folder in week-05 (default {DEFAULT_ORG})")


# ---------------------------------------------------------------------------
# Setup: key, clients, prompts, names
# ---------------------------------------------------------------------------

def load_api_key():
    for path in KEY_FILES:
        if path.is_file():
            key = path.read_text(encoding="utf-8-sig").strip()
            if key:
                return key
    raise SpecialistError(f"No API key found. Save it in {KEY_FILES[0]} (outside the repository).")


_clients = {}


def client():
    if "sync" not in _clients:
        _clients["sync"] = anthropic.Anthropic(api_key=load_api_key())
    return _clients["sync"]


def async_client():
    # Made fresh for each analyst run: an async client belongs to one event loop.
    return anthropic.AsyncAnthropic(api_key=load_api_key())


def system_prompt(name):
    """Read from the organization's prompts folder on every run."""
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


def customer_names():
    return sorted({o["customer"] for o in supply_tools.read_orders() if o["customer"]})


def supplier_names():
    """Supplier company names, their short names (first word) and contact names."""
    names = set()
    for r in supply_tools.read_table("suppliers.csv"):
        names.update(n for n in (r["supplier"], r["supplier"].split()[0], r["contact_name"]) if n)
    for r in supply_tools.read_table("parts.csv"):
        names.add(r["supplier"])
    return sorted(names)


def names_in(text, names):
    """The names that appear in text, as whole words, ignoring case."""
    return [n for n in names
            if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.IGNORECASE)]


# ---------------------------------------------------------------------------
# Printing handoffs, drafts and tokens
# ---------------------------------------------------------------------------

TOKENS = {}  # specialist -> {"calls", "input", "output"} for this Python process


def as_text(payload):
    return payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False)


def handoff(sender, receiver, what, payload):
    print(f"\n=== HANDOFF  {sender} -> {receiver}: {what}")
    print(as_text(payload).strip())


def describe_draft(draft):
    """A stand-in for a draft in a handoff; the final draft is printed once, in full, after
    its review."""
    return f"({len(draft.split())} words; the final draft is printed after the review)"


def print_final(recipient, order_id, draft, needs_person):
    label = "FINAL DRAFT TO SUPPLIER" if recipient == "supplier" else f"FINAL DRAFT TO CUSTOMER {order_id}"
    print("\n" + label)
    print(draft.strip())
    print(f"Marked for a person: {'yes' if needs_person else 'no'}")


def count_tokens(specialist, response):
    t = TOKENS.setdefault(specialist, {"calls": 0, "input": 0, "output": 0})
    t["calls"] += 1
    t["input"] += response.usage.input_tokens
    t["output"] += response.usage.output_tokens
    print(f"    [tokens] {specialist}: {response.usage.input_tokens} input, "
          f"{response.usage.output_tokens} output this call "
          f"(total {t['input']} input, {t['output']} output in {t['calls']} calls)")


def print_token_report():
    print("\n=== TOKENS USED BY EACH SPECIALIST")
    total_in = total_out = 0
    for name, t in TOKENS.items():
        print(f"  {name:<16} {t['calls']:>2} calls  {t['input']:>7} input  {t['output']:>6} output")
        total_in += t["input"]
        total_out += t["output"]
    print(f"  {'all':<16}           {total_in:>7} input  {total_out:>6} output")


def text_of(response, specialist):
    if response.stop_reason == "refusal":
        raise SpecialistError(f"{specialist}: Claude declined the request.")
    if response.stop_reason == "max_tokens":
        raise SpecialistError(f"{specialist}: the answer hit the max_tokens limit.")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def one_call(specialist, prompt_name, user_content):
    """One model call with the specialist's system prompt. Returns its text."""
    response = client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt(prompt_name),
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": user_content}],
    )
    count_tokens(specialist, response)
    return text_of(response, specialist)


# ---------------------------------------------------------------------------
# 1. The supply analyst: an agent with the MCP server's tools
# ---------------------------------------------------------------------------

def run_analyst(email):
    """Work out how a supplier email affects customer orders.

    Returns the brief (a dict), reconciled in code against every check_coverage result
    the analyst got (see reconcile_brief)."""
    handoff("coordinator", "analyst", f"supplier email ({ORG})", email)
    brief, checks = asyncio.run(_analyst(email))
    handoff("analyst", "coordinator", "full brief (JSON)", brief)
    reconciled = reconcile_brief(brief, checks)
    rec = reconciled["reconciliation"]
    handoff("code", "coordinator", f"brief checked against {rec['check_coverage_results']} "
            "check_coverage result(s)",
            {"corrections": rec["corrections"] or "none", "problems": rec["problems"] or "none"})
    if rec["corrections"] or rec["problems"]:
        handoff("code", "coordinator", "reconciled brief (JSON)", reconciled)
    return reconciled


# ---------------------------------------------------------------------------
# Reconciling the brief with the analyst's own check_coverage results
# ---------------------------------------------------------------------------

def short_orders(result):
    """{order_id: row} for the orders a check_coverage result shows short."""
    return {str(r["order_id"]).upper(): r for r in result.get("orders", []) if r.get("status") == "short"}


def match_check(results, ids, allow_superset=True):
    """The check_coverage result for a schedule whose orders at risk are ids.

    Prefers results whose short orders are exactly ids; otherwise the one distinct result
    whose short orders include ids (the analyst left some out). Returns (result, why not)."""
    def distinct(rs):
        seen = {}
        for r in rs:
            seen.setdefault(json.dumps({k: (v["units_short"], v["ready_day"])
                                        for k, v in sorted(short_orders(r).items())}), r)
        return list(seen.values())

    exact = distinct([r for r in results if set(short_orders(r)) == ids])
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "check_coverage results disagree on the numbers for these orders"
    supers = distinct([r for r in results if set(short_orders(r)) > ids]) if allow_superset else []
    if len(supers) == 1:
        return supers[0], None
    if len(supers) > 1:
        return None, "more than one check_coverage result could match"
    return None, "no check_coverage result shows these orders at risk"


def reconcile_brief(brief, checks):
    """The brief with orders_at_risk and options made to agree with check_coverage.

    For each supplier option (or the one new schedule if there are no options), the
    matching check_coverage result decides which orders are at risk and by how much. An
    order at risk under any option is in orders_at_risk; with options, each order keeps
    its option-specific impact in by_option, and its units_short/ready_day are the worst
    case. Customer, ship day and late penalty come from orders.csv. Every change is listed
    in reconciliation.corrections; anything that can't be matched to a tool result is in
    reconciliation.problems (which marks every draft for a person)."""
    brief = copy.deepcopy(brief)
    csv_orders = {o["order_id"].upper(): o for o in supply_tools.read_orders()}
    part = str(brief.get("part_id", "")).upper()
    results = [c for c in checks if str(c.get("part_id", "")).upper() == part]
    analyst = {str(o.get("order_id", "")).upper(): o for o in brief.get("orders_at_risk", [])}
    options = brief.get("options") or []
    corrections, problems = [], []

    scenarios = options or [{"name": None, "order_ids": list(analyst)}]
    impacts = {}  # order_id -> {option name or None: {units_short, ready_day, days_late}}
    for sc in scenarios:
        ids = {str(i).strip().upper() for i in sc.get("order_ids", [])}
        label = f"option '{sc['name']}'" if sc["name"] is not None else "the new schedule"
        if not results:
            if ids:
                problems.append(f"{label}: orders at risk {sorted(ids)} but no check_coverage result")
            continue
        # With no options and nothing at risk, don't pull in orders from other checks.
        result, why = match_check(results, ids, allow_superset=bool(ids) or bool(options))
        if result is None:
            if ids or options:
                problems.append(f"{label}: {why}")
            continue
        short = short_orders(result)
        if set(short) - ids:
            corrections.append(f"{label}: added {sorted(set(short) - ids)}, which check_coverage "
                               "shows at risk")
        if options:
            sc["order_ids"] = sorted(short)
        for oid, r in short.items():
            impacts.setdefault(oid, {})[sc["name"]] = {
                "units_short": r["units_short"], "ready_day": r["ready_day"], "days_late": r["days_late"]}

    for oid in sorted(set(analyst) - set(impacts)):
        problems.append(f"{oid} was listed at risk, but no matching check_coverage result shows it "
                        "short; it was left out")

    def worst(per):
        return max(per.values(), key=lambda v: float("inf") if v["ready_day"] is None else v["days_late"])

    new_orders = []
    for oid, per in impacts.items():
        if oid not in csv_orders:
            problems.append(f"{oid} is not an order in orders.csv")
            continue
        o, w = csv_orders[oid], worst(per)
        entry = {"order_id": o["order_id"], "customer": o["customer"], "units_short": w["units_short"],
                 "ship_day": o["ship_day"], "ready_day": w["ready_day"], "late_penalty": o["late_penalty"]}
        if options:
            entry["by_option"] = per
        old = analyst.get(oid)
        if old is None:
            corrections.append(f"{oid}: added to orders_at_risk from check_coverage")
        elif any(old.get(k) != entry[k] for k in ("units_short", "ship_day", "ready_day", "late_penalty")):
            corrections.append(f"{oid}: corrected to check_coverage's numbers "
                               f"(units_short {entry['units_short']}, ready_day {entry['ready_day']})")
        new_orders.append(entry)
    new_orders.sort(key=lambda e: (e["ship_day"], e["order_id"]))

    brief["orders_at_risk"] = new_orders
    brief["reconciliation"] = {"check_coverage_results": len(results),
                               "corrections": corrections, "problems": problems}
    return brief


async def _analyst(email):
    server = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT), "--org", ORG])
    claude = async_client()
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = [{"name": t.name, "description": t.description or "",
                      "input_schema": t.input_schema}
                     for t in (await mcp.list_tools()).tools]
            messages = [{"role": "user", "content":
                         "Here is the supplier email. Today is day 0.\n\n" + email}]
            tool_calls = 0
            checks = []  # every check_coverage result, for reconcile_brief

            while True:
                at_limit = tool_calls >= MAX_TOOL_CALLS
                response = await claude.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt("analyst"),
                    thinking={"type": "adaptive"},
                    tools=tools,
                    # After 10 tool calls the analyst may only write its brief.
                    tool_choice={"type": "none"} if at_limit else {"type": "auto"},
                    output_config={"format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
                    messages=messages,
                )
                count_tokens("analyst", response)

                if response.stop_reason != "tool_use":
                    text = text_of(response, "analyst")
                    try:
                        return json.loads(text), checks
                    except json.JSONDecodeError:
                        raise SpecialistError(f"analyst: the brief was not valid JSON:\n{text}") from None

                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in (b for b in response.content if b.type == "tool_use"):
                    if tool_calls >= MAX_TOOL_CALLS:
                        results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                        "content": f"Limit of {MAX_TOOL_CALLS} tool calls reached. "
                                                   "Write your brief now from the results you have."})
                        continue
                    tool_calls += 1
                    handoff("analyst", f"MCP tool {block.name}", f"tool call {tool_calls} of "
                            f"{MAX_TOOL_CALLS}", block.input)
                    result = await mcp.call_tool(block.name, block.input)
                    text = "\n".join(c.text for c in result.content if c.type == "text")
                    print(f"    [result{' (error)' if result.is_error else ''}] {len(text)} characters")
                    if block.name == "check_coverage" and not result.is_error:
                        try:
                            checks.append(json.loads(text))
                        except json.JSONDecodeError:
                            pass
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": text, "is_error": bool(result.is_error)})
                if tool_calls >= MAX_TOOL_CALLS:
                    print(f"    [limit] analyst reached {MAX_TOOL_CALLS} tool calls; asking for the brief")
                messages.append({"role": "user", "content": results})


# ---------------------------------------------------------------------------
# 2. The supplier writer
# ---------------------------------------------------------------------------

IMPACT_NOTE = ("Worked out in code from the analyst's order data. State the impact only with "
               "these numbers (how many orders are delayed, and the fewest to the most days "
               "late); don't work out your own.")
IMPACT_NOTE_OPTIONS = ("Worked out in code from the analyst's order data. State each option's impact "
                       "separately with these numbers (how many orders it delays, and the fewest to "
                       "the most days late); never combine options into one figure, and don't work "
                       "out your own.")


def validated_delays(brief):
    """The analyst's orders at risk, checked against orders.csv.

    Returns ({order_id: days late, or None if there is no new ship date}, problems). An
    order is used only if it is in orders.csv; its ship day comes from orders.csv, its
    ready day from the brief (which came from check_coverage)."""
    ship_days = {o["order_id"].upper(): o["ship_day"] for o in supply_tools.read_orders()}
    delays, problems = {}, []
    for o in (brief or {}).get("orders_at_risk", []):
        oid = str(o.get("order_id", "")).strip().upper()
        if oid not in ship_days:
            problems.append(f"{oid or '(blank)'} is not an order in orders.csv")
            continue
        if oid in delays:
            problems.append(f"{oid} is listed twice")
            continue
        ship, ready = ship_days[oid], o.get("ready_day")
        if o.get("ship_day") is not None and int(o["ship_day"]) != ship:
            problems.append(f"{oid}: the brief's ship day {o['ship_day']} is not orders.csv's {ship}")
        if ready is None:
            delays[oid] = None
        elif int(ready) <= ship:
            problems.append(f"{oid}: ready day {ready} is not after its ship day {ship}")
        else:
            delays[oid] = int(ready) - ship
    return delays, problems


def summarize_delays(delays, order_ids=None):
    """How many of these orders are delayed, and the fewest and most days late."""
    if order_ids is not None:
        wanted = {i.strip().upper() for i in order_ids}
        delays = {k: v for k, v in delays.items() if k in wanted}
    known = [d for d in delays.values() if d is not None]
    out = {"orders_delayed": len(delays)}
    if known:
        out.update(days_late_fewest=min(known), days_late_most=max(known))
    if len(known) < len(delays):
        out["orders_with_no_new_ship_date"] = len(delays) - len(known)
    return out


def delay_impact(brief):
    """(impact overall, [impact for each option], problems), computed in code.

    Overall uses each order's worst case; each option uses that order's option-specific
    days late (by_option, from reconcile_brief) where there is one."""
    brief = brief or {}
    delays, problems = validated_delays(brief)
    problems = problems + (brief.get("reconciliation") or {}).get("problems", [])
    at_risk = {str(o.get("order_id", "")).upper(): o for o in brief.get("orders_at_risk", [])}
    options = []
    for opt in brief.get("options") or []:
        name, per = opt.get("name", ""), {}
        for oid in {str(i).strip().upper() for i in opt.get("order_ids", [])}:
            if oid in delays:
                by = (at_risk[oid].get("by_option") or {}).get(name)
                per[oid] = by["days_late"] if by else delays[oid]
        options.append({"name": name, **summarize_delays(per)})
    return summarize_delays(delays), options, problems


CHOOSES_OPTION = re.compile(
    r"\b(?:we(?:'ll|'d| will| would)?(?: like to)? (?:choose|prefer|select|accept|go with|opt for|take)\b[^.\n]*option"
    r"|please (?:proceed|go ahead|ship)(?: with)? option"
    r"|option [a-z0-9]+ (?:works|is (?:our )?(?:preferred|choice))"
    r"|(?:confirm|approve|agree to|accept) option [a-z0-9]+)", re.IGNORECASE)


DAY_RANGE = re.compile(r"(\d+)\s*(?:days?\s+)?(?:to|-|–|—|and)\s*(\d+)\s+(?:more\s+|business\s+)?days?",
                       re.IGNORECASE)
UP_TO = re.compile(r"up\s+to\s+(\d+)\s+(?:more\s+|business\s+)?days?", re.IGNORECASE)


def wrong_day_ranges(draft, groups):
    """Delay ranges in a draft ("2 to 7 days", "up to 7 days") that match none of the
    computed impacts in groups. Returns the wrong phrases."""
    ranges = {(g["days_late_fewest"], g["days_late_most"]) for g in groups if "days_late_most" in g}
    if not ranges:
        return []
    mosts = {most for _, most in ranges}
    wrong = [m.group(0) for m in DAY_RANGE.finditer(draft)
             if (int(m.group(1)), int(m.group(2))) not in ranges]
    wrong += [m.group(0) for m in UP_TO.finditer(draft) if int(m.group(1)) not in mosts]
    return wrong


def for_supplier(email, brief):
    """The email and a sanitized brief for the supplier side.

    Only these fields go: the part, what changed, the impact computed in code (orders
    delayed, fewest and most days late), each option with its impact, and the supplier's
    questions. No customer names, order IDs, late penalties, per-order details or internal
    decisions; any customer name or order ID inside the text is replaced."""
    names = customer_names()
    order_ids = sorted({o["order_id"] for o in supply_tools.read_orders()}, key=len, reverse=True)

    def scrub(text):
        text = str(text)
        for n in names:
            text = re.sub(re.escape(n), "a customer", text, flags=re.IGNORECASE)
        for oid in order_ids:
            text = re.sub(r"(?<!\w)" + re.escape(oid) + r"(?!\w)", "an order", text, flags=re.IGNORECASE)
        return text

    impact, options, _ = delay_impact(brief)
    clean_brief = {
        "part_id": (brief or {}).get("part_id", ""),
        "what_changed": scrub((brief or {}).get("what_changed", "")),
        # With options there is no single impact: a combined figure would describe no
        # option that can actually be chosen.
        "impact": impact if not options else "Depends on the option chosen; see each option's impact.",
        "impact_note": IMPACT_NOTE if not options else IMPACT_NOTE_OPTIONS,
        "options": [{**o, "name": scrub(o["name"])} for o in options],
        "supplier_questions": [scrub(q) for q in (brief or {}).get("supplier_questions", [])],
    }
    return scrub(email), clean_brief


def revision_note(previous, feedback):
    if not previous:
        return ""
    return ("\n\nYour previous draft:\n" + previous +
            "\n\nThe reviewer asked for these changes. Write the full corrected draft:\n" + feedback)


def write_supplier_reply(email, brief, previous=None, feedback=None):
    """One model call. Customer names, late penalties and internal decisions are removed
    from the email and brief first."""
    email, brief = for_supplier(email, brief)
    content = ("Supplier email:\n" + email + "\n\nSupply analyst's brief:\n" + as_text(brief)
               + revision_note(previous, feedback))
    handoff("coordinator", "supplier writer",
            "email and sanitized brief (no customer names, order IDs, penalties or internal "
            "decisions; impact computed in code)"
            + (" + reviewer feedback" if feedback else ""),
            content if not feedback else
            "Same email and brief as before, the previous draft, and the reviewer's feedback.")
    draft = one_call("supplier writer", "supplier-writer", content)
    handoff("supplier writer", "coordinator", "supplier draft", describe_draft(draft))
    return draft


# ---------------------------------------------------------------------------
# 3. The customer writer
# ---------------------------------------------------------------------------

def delay_is_conditional(brief, order_id):
    """True if the supplier offered options and this order's delay depends on which one is
    chosen: some option keeps it on time, or the options delay it by different amounts.

    Then the delay depends on a decision the organization has not made yet."""
    options = (brief or {}).get("options") or []
    if not options:
        return False
    order_id = order_id.strip().upper()
    if any(order_id not in {str(i).strip().upper() for i in o.get("order_ids", [])} for o in options):
        return True
    order = next((o for o in brief.get("orders_at_risk", [])
                  if str(o.get("order_id", "")).upper() == order_id), {})
    days = {v.get("days_late") for v in (order.get("by_option") or {}).values()}
    return len(days) > 1


def order_facts(order, conditional=False):
    """One order's facts from orders.csv, with the new ready day from the brief.

    order needs order_id and ready_day (as in the brief's orders_at_risk). If conditional,
    the facts say the delay is not certain. Returns (facts for the writer, whether the
    order has a late penalty)."""
    order_id = str(order.get("order_id", "")).strip().upper()
    row = next((o for o in supply_tools.read_orders() if o["order_id"].upper() == order_id), None)
    if row is None:
        raise SpecialistError(f"No order {order_id!r} in orders.csv.")
    ready = order.get("ready_day")
    # Only days late: the internal day numbers (day 0 = today) mean nothing to a customer.
    facts = {
        "order_id": row["order_id"],
        "customer": row["customer"],
        "product": row["product"],
        "quantity": row["quantity"],
        "days_late": None if ready is None else int(ready) - row["ship_day"],
        "cause": "a delay in a part from one of our suppliers",
        "note": "The new ship date is not known yet." if ready is None else "",
    }
    if conditional:
        facts["delay_is_certain"] = False
        facts["note"] = (facts["note"] + " The delay is NOT certain: it depends on a decision "
                         "we have not made yet. Say the order may ship up to the number of days "
                         "late shown, never that it will, and that we will confirm the ship "
                         "date.").strip()
    if not facts["note"]:
        del facts["note"]
    return facts, row["late_penalty"]


def write_customer_notice(order, previous=None, feedback=None, conditional=False):
    """One model call, given only that one order's facts.

    conditional=True tells the writer the delay is possible, not certain."""
    facts, _ = order_facts(order, conditional)
    content = "Order facts:\n" + as_text(facts) + revision_note(previous, feedback)
    handoff("coordinator", "customer writer",
            f"facts for order {facts['order_id']}" + (" + reviewer feedback" if feedback else ""),
            content if not feedback else
            "Same order facts as before, the previous draft, and the reviewer's feedback.")
    draft = one_call("customer writer", "customer-writer", content)
    handoff("customer writer", "coordinator", f"customer draft for {facts['order_id']}",
            describe_draft(draft))
    return draft


# ---------------------------------------------------------------------------
# 4. The reviewer
# ---------------------------------------------------------------------------

def review(draft, facts, recipient):
    """One model call. Returns {"verdict": "APPROVE" or "REVISE", "reasons": text}."""
    content = (f"Recipient: {recipient}\n\nFacts the draft was written from:\n{as_text(facts)}"
               f"\n\nDraft:\n{draft}")
    handoff("coordinator", "reviewer", f"{recipient} draft and its facts",
            f"Facts:\n{as_text(facts)}\nDraft: the latest {recipient} draft {describe_draft(draft)}")
    text = one_call("reviewer", "reviewer", content)
    first, _, rest = text.partition("\n")
    verdict = "APPROVE" if first.strip().strip("*#: ").upper().startswith("APPROVE") else "REVISE"
    result = {"verdict": verdict, "reasons": rest.strip() if verdict == "APPROVE" else text}
    handoff("reviewer", "coordinator", f"verdict {verdict}", result["reasons"] or "(no reasons given)")
    return result


# ---------------------------------------------------------------------------
# The review step: writer -> reviewer -> up to 2 revisions -> name check -> print -> save
# ---------------------------------------------------------------------------

def write_and_review(write, facts, recipient):
    """Run a writer, then the reviewer, revising at most MAX_REVISIONS times."""
    draft = write()
    if draft.strip() == NO_REPLY:
        return draft, {"verdict": "APPROVE", "reasons": "Writer found no reply needed."}, 0
    revisions = 0
    verdict = review(draft, facts, recipient)
    while verdict["verdict"] == "REVISE" and revisions < MAX_REVISIONS:
        revisions += 1
        print(f"\n--- revision {revisions} of {MAX_REVISIONS}")
        draft = write(previous=draft, feedback=verdict["reasons"])
        verdict = review(draft, facts, recipient)
    return draft, verdict, revisions


def save_draft(kind, label, recipient, draft, verdict, revisions, reasons, notes=()):
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    needs_person = bool(reasons)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{kind}_{label}{'_NEEDS-PERSON' if needs_person else ''}.txt"
    path = DRAFTS_DIR / name
    header = [f"Organization: {ORG}",
              f"To: {recipient}",
              *notes,
              f"Reviewer: {verdict['verdict']} after {revisions} revision(s)",
              f"Needs a person: {'YES' if needs_person else 'no'}"]
    header += [f"  - {r}" for r in reasons]
    path.write_text("\n".join(header) + "\n" + "-" * 60 + "\n" + draft.strip() + "\n", encoding="utf-8")
    shown = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)
    print(f"\n=== SAVED  {shown}")
    for r in reasons:
        print(f"    - {r}")
    return path


def supplier_reasons(draft, verdict, revisions, brief):
    """Why a supplier draft needs a person (empty if it doesn't)."""
    reasons = []
    if verdict["verdict"] != "APPROVE":
        reasons.append(f"Reviewer still asks for changes after {revisions} revisions.")
    found = names_in(draft, customer_names())
    if found:
        reasons.append("Names a customer: " + ", ".join(found))
    if re.search(r"penalt", draft, re.IGNORECASE):
        reasons.append("Mentions a penalty (our contract terms).")
    impact, options, problems = delay_impact(brief)
    if problems:
        reasons.append("The analyst's order data didn't check out against orders.csv and its "
                       "check_coverage results: " + "; ".join(problems))
    # With options, only each option's own range is right; a combined range is not.
    wrong = wrong_day_ranges(draft, options if options else [impact])
    if wrong:
        reasons.append("States a delay range that conflicts with the computed facts: "
                       + "; ".join(f'"{w}"' for w in wrong))
    if options:
        chose = CHOOSES_OPTION.search(draft)
        if chose:
            reasons.append(f'May choose an option on our behalf: "{chose.group(0)}"')
        if draft.strip() != NO_REPLY:
            reasons.append("The supplier offered options; a person must choose one before this is sent.")
    penalty_orders = [o["order_id"] for o in brief.get("orders_at_risk", []) if o.get("late_penalty")]
    if penalty_orders and draft.strip() != NO_REPLY:
        reasons.append("Affects orders with a late penalty: " + ", ".join(penalty_orders))
    return reasons


def customer_reasons(draft, verdict, revisions, facts, late_penalty, brief, conditional):
    """Why a customer draft needs a person (empty if it doesn't)."""
    reasons = []
    if (brief or {}).get("options"):
        reasons.append("CONDITIONAL NOTICE: the supplier offered options and none is chosen yet; "
                       + ("this delay depends on the option chosen."
                          if conditional else "check the notice still fits the option chosen."))
    problems = ((brief or {}).get("reconciliation") or {}).get("problems", [])
    if problems:
        reasons.append("The analyst's brief didn't fully check out against its check_coverage "
                       "results: " + "; ".join(problems))
    if verdict["verdict"] != "APPROVE":
        reasons.append(f"Reviewer still asks for changes after {revisions} revisions.")
    found = names_in(draft, supplier_names())
    if found:
        reasons.append("Names a supplier: " + ", ".join(found))
    others = names_in(draft, [n for n in customer_names() if n != facts["customer"]])
    if others:
        reasons.append("Names another customer: " + ", ".join(others))
    if late_penalty:
        reasons.append("Order has a late penalty.")
    return reasons


def reviewed_supplier_reply(email, brief):
    """Supplier draft, reviewed, name-checked, printed and saved. Returns a result dict."""
    clean_email, clean_brief = for_supplier(email, brief)
    facts = {"supplier_email": clean_email, "brief": clean_brief}
    draft, verdict, revisions = write_and_review(
        lambda previous=None, feedback=None: write_supplier_reply(email, brief, previous, feedback),
        facts, "supplier")

    reasons = supplier_reasons(draft, verdict, revisions, brief)
    print_final("supplier", None, draft, bool(reasons))
    path = save_draft("supplier", brief.get("part_id", "part"), "supplier", draft,
                      verdict, revisions, reasons)
    return {"recipient": "supplier", "draft": draft, "verdict": verdict["verdict"],
            "revisions": revisions, "needs_person": bool(reasons), "reasons": reasons,
            "path": str(path)}


def reviewed_customer_notice(order, brief=None):
    """Customer draft for one order at risk, reviewed, name-checked, printed and saved.

    Pass the analyst's brief so a delay that depends on the supplier's options is written
    as possible, not certain, and the draft is marked for a person."""
    conditional = delay_is_conditional(brief, str(order.get("order_id", "")))
    facts, late_penalty = order_facts(order, conditional)
    draft, verdict, revisions = write_and_review(
        lambda previous=None, feedback=None: write_customer_notice(order, previous, feedback,
                                                                   conditional),
        facts, "customer")

    reasons = customer_reasons(draft, verdict, revisions, facts, late_penalty, brief, conditional)
    notes = []
    if conditional:
        by = next((o.get("by_option") for o in brief.get("orders_at_risk", [])
                   if o["order_id"].upper() == facts["order_id"].upper()), None) or {}
        per = "; ".join(f"{name}: {v['days_late']} days late" for name, v in by.items())
        notes.append("Conditional notice: YES - the delay depends on a supplier option not yet "
                     "chosen" + (f" ({per}; on time under any other option)" if per else ""))
    print_final("customer", facts["order_id"], draft, bool(reasons))
    path = save_draft("customer", facts["order_id"], f"{facts['customer']} (order {facts['order_id']})",
                      draft, verdict, revisions, reasons, notes)
    return {"recipient": "customer", "order_id": facts["order_id"], "draft": draft,
            "verdict": verdict["verdict"], "revisions": revisions,
            "needs_person": bool(reasons), "reasons": reasons, "path": str(path)}


# ---------------------------------------------------------------------------
# Try it: analyst -> supplier reply -> one notice per order at risk
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run the specialists on one supplier email.")
    add_org_argument(parser)
    parser.add_argument("email", type=Path, help="path of one supplier email")
    args = parser.parse_args()
    try:
        set_org(args.org)
        email = args.email.read_text(encoding="utf-8")
        brief = run_analyst(email)
        reviewed_supplier_reply(email, brief)
        for order in brief["orders_at_risk"]:
            reviewed_customer_notice(order, brief)
    except (SpecialistError, supply_tools.SupplyError) as exc:
        print(f"\nStopped: {exc}")
    except OSError as exc:
        print(f"\nStopped: {exc}")
    except anthropic.AuthenticationError:
        print("\nStopped: Claude rejected the API key.")
    except anthropic.APIStatusError as exc:
        print(f"\nStopped: Claude API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        print("\nStopped: could not reach the Claude API.")
    finally:
        print_token_report()


if __name__ == "__main__":
    main()
