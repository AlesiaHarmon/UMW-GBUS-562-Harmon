"""Brookfield Bicycle Company's four specialists, shared by the coordinators in week-05.

    run_analyst(email)                 agent with the tools of supply_mcp_server.py; at most
                                       10 tool calls; returns its brief as a dict (JSON)
    write_supplier_reply(email, brief) one model call; customer names are removed first
    write_customer_notice(order)       one model call, given only that one order's facts
    review(draft, facts, recipient)    one model call; returns APPROVE or REVISE with reasons

The review step runs a writer, then the reviewer, and revises at most 2 times:

    reviewed_supplier_reply(email, brief)
    reviewed_customer_notice(order, brief)

If the supplier offered options and one of them keeps the order on time, the customer
writer is told the delay is possible, not certain. Any customer notice written while the
supplier's options are still open is marked for a person.

After the reviewer, code checks that a supplier draft names no customer and a customer
draft names no supplier (or other customer). Every draft is saved in week-05/drafts. A
draft is marked for a person if the reviewer did not approve it, the name check failed,
its order has a late penalty, or (customer notices) the supplier's options are still open.

Each specialist reads its system prompt from week-05/brookfield-bikes/prompts on every
run, so an edited prompt is picked up without restarting anything. Every handoff and the
tokens each specialist used are printed.

The API key is read from API-Key.txt (or "API Key.txt") in the folder above the
repository. It is only held in memory, never written anywhere.

Try it on one sample email (this calls Claude and costs a few cents):
    .venv\\Scripts\\python.exe week-05\\specialists.py week-05\\brookfield-bikes\\supplier-emails\\01-northgate-controller-delay.eml
"""

import asyncio
import copy
import csv
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
DATA_DIR = HERE / "brookfield-bikes"
PROMPTS_DIR = DATA_DIR / "prompts"
DRAFTS_DIR = HERE / "drafts"
SERVER_SCRIPT = HERE / "supply_mcp_server.py"

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
# Setup: key, clients, prompts, data
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
    """Read on every run, so edits to the prompt files take effect at once."""
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


def read_csv(name):
    with (DATA_DIR / name).open(encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def customer_names():
    return sorted({r["customer"] for r in read_csv("orders.csv") if r["customer"]})


def supplier_names():
    """Supplier company names, their short names (first word) and contact names."""
    names = set()
    for r in read_csv("suppliers.csv"):
        names.update(n for n in (r["supplier"], r["supplier"].split()[0], r["contact_name"]) if n)
    for r in read_csv("parts.csv"):
        names.add(r["supplier"])
    return sorted(names)


def names_in(text, names):
    """The names that appear in text, as whole words, ignoring case."""
    return [n for n in names
            if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.IGNORECASE)]


# ---------------------------------------------------------------------------
# Printing handoffs and tokens
# ---------------------------------------------------------------------------

TOKENS = {}  # specialist -> {"calls", "input", "output"} for this Python process


def as_text(payload):
    return payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False)


def handoff(sender, receiver, what, payload):
    print(f"\n=== HANDOFF  {sender} -> {receiver}: {what}")
    print(as_text(payload).strip())


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
    """Work out how a supplier email affects customer orders. Returns the brief (a dict)."""
    handoff("coordinator", "analyst", "supplier email", email)
    brief = asyncio.run(_analyst(email))
    handoff("analyst", "coordinator", "brief (JSON)", brief)
    return brief


async def _analyst(email):
    server = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])
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
                        return json.loads(text)
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
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": text, "is_error": bool(result.is_error)})
                if tool_calls >= MAX_TOOL_CALLS:
                    print(f"    [limit] analyst reached {MAX_TOOL_CALLS} tool calls; asking for the brief")
                messages.append({"role": "user", "content": results})


# ---------------------------------------------------------------------------
# 2. The supplier writer
# ---------------------------------------------------------------------------

# Brief fields the supplier side never sees: who the customers are, which orders carry a
# late penalty (Brookfield's contract terms), and Brookfield's internal decisions.
SUPPLIER_HIDDEN_FIELDS = {"customer", "late_penalty", "decisions_for_a_person"}


def for_supplier(email, brief):
    """The email and brief for the supplier side: customer names removed, and no late
    penalties or internal decisions."""
    names = customer_names()

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in SUPPLIER_HIDDEN_FIELDS}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            for n in names:
                value = re.sub(re.escape(n), "a customer", value, flags=re.IGNORECASE)
        return value

    return scrub(email), scrub(copy.deepcopy(brief))


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
            "email and brief, without customer names, penalties or internal decisions"
            + (" + reviewer feedback" if feedback else ""),
            content)
    draft = one_call("supplier writer", "supplier-writer", content)
    handoff("supplier writer", "coordinator", "supplier draft", draft)
    return draft


# ---------------------------------------------------------------------------
# 3. The customer writer
# ---------------------------------------------------------------------------

def delay_is_conditional(brief, order_id):
    """True if the supplier offered options and at least one of them keeps this order on time.

    Then the delay depends on a decision Brookfield has not made yet."""
    options = (brief or {}).get("options") or []
    order_id = order_id.strip().upper()
    return any(order_id not in {i.strip().upper() for i in o.get("order_ids", [])} for o in options)


def order_facts(order, conditional=False):
    """One order's facts from orders.csv, with the new ready day from the brief.

    order needs order_id and ready_day (as in the brief's orders_at_risk). If conditional,
    the facts say the delay is not certain. Returns (facts for the writer, whether the
    order has a late penalty)."""
    order_id = str(order.get("order_id", "")).strip().upper()
    row = next((r for r in read_csv("orders.csv") if r["order_id"].upper() == order_id), None)
    if row is None:
        raise SpecialistError(f"No order {order_id!r} in orders.csv.")
    planned = int(row["ship_in_days"])
    ready = order.get("ready_day")
    # Only days late: the internal day numbers (day 0 = today) mean nothing to a customer.
    facts = {
        "order_id": row["order_id"],
        "customer": row["customer"],
        "model": row["model"],
        "bikes": int(row["qty"]),
        "days_late": None if ready is None else int(ready) - planned,
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
    return facts, row["late_penalty"].lower() == "yes"


def write_customer_notice(order, previous=None, feedback=None, conditional=False):
    """One model call, given only that one order's facts.

    conditional=True tells the writer the delay is possible, not certain."""
    facts, _ = order_facts(order, conditional)
    content = "Order facts:\n" + as_text(facts) + revision_note(previous, feedback)
    handoff("coordinator", "customer writer",
            f"facts for order {facts['order_id']}" + (" + reviewer feedback" if feedback else ""),
            content)
    draft = one_call("customer writer", "customer-writer", content)
    handoff("customer writer", "coordinator", f"customer draft for {facts['order_id']}", draft)
    return draft


# ---------------------------------------------------------------------------
# 4. The reviewer
# ---------------------------------------------------------------------------

def review(draft, facts, recipient):
    """One model call. Returns {"verdict": "APPROVE" or "REVISE", "reasons": text}."""
    content = (f"Recipient: {recipient}\n\nFacts the draft was written from:\n{as_text(facts)}"
               f"\n\nDraft:\n{draft}")
    handoff("coordinator", "reviewer", f"{recipient} draft and its facts", content)
    text = one_call("reviewer", "reviewer", content)
    first, _, rest = text.partition("\n")
    verdict = "APPROVE" if first.strip().strip("*#: ").upper().startswith("APPROVE") else "REVISE"
    result = {"verdict": verdict, "reasons": rest.strip() if verdict == "APPROVE" else text}
    handoff("reviewer", "coordinator", f"verdict {verdict}", result["reasons"] or "(no reasons given)")
    return result


# ---------------------------------------------------------------------------
# The review step: writer -> reviewer -> up to 2 revisions -> name check -> save
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


def save_draft(kind, label, recipient, draft, verdict, revisions, reasons):
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    needs_person = bool(reasons)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{kind}_{label}{'_NEEDS-PERSON' if needs_person else ''}.txt"
    path = DRAFTS_DIR / name
    header = [f"To: {recipient}",
              f"Reviewer: {verdict['verdict']} after {revisions} revision(s)",
              f"Needs a person: {'YES' if needs_person else 'no'}"]
    header += [f"  - {r}" for r in reasons]
    path.write_text("\n".join(header) + "\n" + "-" * 60 + "\n" + draft.strip() + "\n", encoding="utf-8")
    print(f"\n=== SAVED  {path.relative_to(REPO_ROOT).as_posix()}"
          f"{'  (marked for a person)' if needs_person else ''}")
    for r in reasons:
        print(f"    - {r}")
    return path


def reviewed_supplier_reply(email, brief):
    """Supplier draft, reviewed, name-checked and saved. Returns a result dict."""
    clean_email, clean_brief = for_supplier(email, brief)
    facts = {"supplier_email": clean_email, "brief": clean_brief}
    draft, verdict, revisions = write_and_review(
        lambda previous=None, feedback=None: write_supplier_reply(email, brief, previous, feedback),
        facts, "supplier")

    reasons = []
    if verdict["verdict"] != "APPROVE":
        reasons.append(f"Reviewer still asks for changes after {revisions} revisions.")
    found = names_in(draft, customer_names())
    if found:
        reasons.append("Names a customer: " + ", ".join(found))
    if re.search(r"penalt", draft, re.IGNORECASE):
        reasons.append("Mentions a penalty (Brookfield's contract terms).")
    penalty_orders = [o["order_id"] for o in brief.get("orders_at_risk", []) if o.get("late_penalty")]
    if penalty_orders and draft.strip() != NO_REPLY:
        reasons.append("Affects orders with a late penalty: " + ", ".join(penalty_orders))

    path = save_draft("supplier", brief.get("part_id", "part"), "supplier", draft,
                      verdict, revisions, reasons)
    return {"recipient": "supplier", "draft": draft, "verdict": verdict["verdict"],
            "revisions": revisions, "needs_person": bool(reasons), "reasons": reasons,
            "path": str(path)}


def reviewed_customer_notice(order, brief=None):
    """Customer draft for one order at risk, reviewed, name-checked and saved.

    Pass the analyst's brief so a delay that depends on the supplier's options is written
    as possible, not certain, and the draft is marked for a person."""
    conditional = delay_is_conditional(brief, str(order.get("order_id", "")))
    facts, late_penalty = order_facts(order, conditional)
    draft, verdict, revisions = write_and_review(
        lambda previous=None, feedback=None: write_customer_notice(order, previous, feedback,
                                                                   conditional),
        facts, "customer")

    reasons = []
    if (brief or {}).get("options"):
        reasons.append("The supplier offered options and none is chosen yet; "
                       + ("this delay happens only under some of them."
                          if conditional else "check the notice still fits the option chosen."))
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

    path = save_draft("customer", facts["order_id"], f"{facts['customer']} (order {facts['order_id']})",
                      draft, verdict, revisions, reasons)
    return {"recipient": "customer", "order_id": facts["order_id"], "draft": draft,
            "verdict": verdict["verdict"], "revisions": revisions,
            "needs_person": bool(reasons), "reasons": reasons, "path": str(path)}


# ---------------------------------------------------------------------------
# Try it: analyst -> supplier reply -> one notice per order at risk
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python week-05/specialists.py <supplier email file>")
    email = Path(sys.argv[1]).read_text(encoding="utf-8")
    try:
        brief = run_analyst(email)
        reviewed_supplier_reply(email, brief)
        for order in brief["orders_at_risk"]:
            reviewed_customer_notice(order, brief)
    except SpecialistError as exc:
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
