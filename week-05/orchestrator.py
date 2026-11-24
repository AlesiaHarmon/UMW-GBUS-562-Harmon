"""An organization's orchestrator: Claude decides which specialists to use.

An agent (claude-sonnet-5) gets the system prompt in <org>/prompts/orchestrator.txt
(--org, default brookfield-bikes) and four tools:
    run_supply_analyst      runs the supply analyst on the email; returns its brief
    draft_supplier_reply    supplier reply from the brief, through the review step
    draft_customer_notice   notice for one order at risk (by order ID), through the review step
    finish                  a summary for a person, and whether a person must decide something
It stops after 10 tool calls. Each choice it makes is printed, then the same summary as
pipeline.py.

From the top of the repository:
    .venv\\Scripts\\python.exe week-05\\orchestrator.py --email week-05\\brookfield-bikes\\supplier-emails\\01-northgate-controller-delay.eml
    .venv\\Scripts\\python.exe week-05\\orchestrator.py --org copperline-coffee --email week-05\\copperline-coffee\\supplier-emails\\01-danforth-pump-delay.eml
Add --save run.txt to also write everything printed to run.txt (UTF-8).
"""

import argparse
import json
import re
import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
import specialists  # noqa: E402
from pipeline import Tee, summary  # noqa: E402
from specialists import SpecialistError  # noqa: E402
from supply_tools import SupplyError  # noqa: E402

MAX_TOOL_CALLS = 10


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or [], "additionalProperties": False}


TOOLS = [
    {
        "name": "run_supply_analyst",
        "description": "Run the supply analyst on the supplier email. It works out how the email "
                       "changes what arrives and when, and which customer orders are at risk. "
                       "Returns its brief as JSON.",
        "input_schema": schema(),
        "strict": True,
    },
    {
        "name": "draft_supplier_reply",
        "description": "Draft a reply to the supplier from the email and the analyst's brief. "
                       "Run the supply analyst first. The draft is reviewed, checked and saved "
                       "for a person; nothing is sent. Returns the draft and the review result.",
        "input_schema": schema(),
        "strict": True,
    },
    {
        "name": "draft_customer_notice",
        "description": "Draft a notice to the customer of one order at risk in the analyst's "
                       "brief. The draft is reviewed, checked and saved for a person; nothing is "
                       "sent. Returns the draft and the review result.",
        "input_schema": schema({"order_id": {"type": "string",
                                             "description": "An order ID from the brief's "
                                                            "orders_at_risk, like O-1043."}},
                               ["order_id"]),
        "strict": True,
    },
    {
        "name": "finish",
        "description": "End the job. Call it once, last.",
        "input_schema": schema({
            "summary": {"type": "string",
                        "description": "A short summary for the purchasing manager."},
            "decision_needed": {"type": "boolean",
                                "description": "True if a person must make a decision."},
        }, ["summary", "decision_needed"]),
        "strict": True,
    },
]


# Tool-call syntax that sometimes leaks into a text field, such as "</parameter>".
MARKUP = re.compile(r"</?\s*(?:parameter|invoke|function_calls|tool_use|antml)\b", re.IGNORECASE)


def finish_problem(args):
    """Why a finish call can't be accepted (None if it can): the summary must be clean
    plain text and decision_needed a real true/false."""
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return "finish needs a non-empty summary. Call finish again with a plain-text summary."
    if MARKUP.search(summary):
        return ("The summary contains tool-call markup (such as </parameter>). Call finish again "
                "with a plain-text summary only, and decision_needed as its own field.")
    if not isinstance(args.get("decision_needed"), bool):
        return "decision_needed must be true or false. Call finish again."
    return None


class Job:
    """What the specialists have produced for one email."""

    def __init__(self, email):
        self.email = email
        self.brief = None
        self.results = []
        self.drafted = set()  # "supplier" and order IDs already drafted
        self.finished = None  # (summary, decision_needed)

    def run_tool(self, name, args):
        """Run one tool. Returns (result text, is_error)."""
        if name == "run_supply_analyst":
            if self.brief is not None:
                return "The analyst already ran. Its brief:\n" + json.dumps(self.brief), True
            self.brief = specialists.run_analyst(self.email)
            return json.dumps(self.brief), False

        if name == "draft_supplier_reply":
            if self.brief is None:
                return "Run the supply analyst first.", True
            if "supplier" in self.drafted:
                return "The supplier reply is already drafted.", True
            result = specialists.reviewed_supplier_reply(self.email, self.brief)
            return self.record("supplier", result), False

        if name == "draft_customer_notice":
            if self.brief is None:
                return "Run the supply analyst first.", True
            order_id = str(args.get("order_id", "")).strip().upper()
            order = next((o for o in self.brief.get("orders_at_risk", [])
                          if o["order_id"].upper() == order_id), None)
            if order is None:
                ids = ", ".join(o["order_id"] for o in self.brief.get("orders_at_risk", [])) or "none"
                return f"{order_id!r} is not an order at risk in the brief. Orders at risk: {ids}.", True
            if order_id in self.drafted:
                return f"The notice for {order_id} is already drafted.", True
            result = specialists.reviewed_customer_notice(order, self.brief)
            return self.record(order_id, result), False

        if name == "finish":
            problem = finish_problem(args)
            if problem:
                return problem, True
            self.finished = (args["summary"].strip(), args["decision_needed"])
            return "Finished.", False

        return f"Unknown tool: {name}", True

    def record(self, key, result):
        self.drafted.add(key)
        self.results.append(result)
        return json.dumps({k: result[k] for k in
                           ("draft", "verdict", "revisions", "needs_person", "reasons")})


def run(email_path):
    """The orchestrator's loop. Returns (job, stop reason or None)."""
    email = email_path.read_text(encoding="utf-8")
    print(f"Organization: {specialists.ORG}")
    print(f"Supplier email: {email_path}")
    job = Job(email)
    client = specialists.client()
    messages = [{"role": "user", "content": "Here is the supplier email:\n\n" + email}]
    tool_calls = 0

    while True:
        try:
            response = client.messages.create(
                model=specialists.MODEL,
                max_tokens=specialists.MAX_TOKENS,
                system=specialists.system_prompt("orchestrator"),
                thinking={"type": "adaptive"},
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.AuthenticationError:
            return job, "Claude rejected the API key."
        except anthropic.RateLimitError:
            return job, "Rate limit reached. Wait a moment and try again."
        except anthropic.APIStatusError as exc:
            return job, f"Claude API error {exc.status_code}: {exc.message}"
        except anthropic.APIConnectionError:
            return job, "Could not reach the Claude API. Check your internet connection."
        specialists.count_tokens("orchestrator", response)

        said = "".join(b.text for b in response.content if b.type == "text").strip()
        if said:
            print(f"\n[orchestrator says] {said}")
        if response.stop_reason != "tool_use":
            return job, {"end_turn": "The orchestrator stopped without calling finish.",
                         "max_tokens": "The orchestrator's answer hit the max_tokens limit.",
                         "refusal": "The orchestrator declined this email."}.get(
                response.stop_reason, f"The orchestrator stopped ({response.stop_reason}).")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in (b for b in response.content if b.type == "tool_use"):
            if tool_calls >= MAX_TOOL_CALLS or job.finished:
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": "Not run: the job is over."})
                continue
            tool_calls += 1
            print("\n" + "#" * 70)
            print(f"# CHOICE {tool_calls} of {MAX_TOOL_CALLS}: {block.name}"
                  + (f" {json.dumps(block.input)}" if block.input else ""))
            print("#" * 70)
            try:
                text, is_error = job.run_tool(block.name, block.input)
            except (SpecialistError, SupplyError) as exc:
                text, is_error = f"The specialist failed: {exc}", True
            except anthropic.APIError as exc:
                return job, f"Claude API error in {block.name}: {exc}"
            if is_error:
                print(f"    [tool error] {text}")
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": text, "is_error": is_error})

        if job.finished:
            return job, None
        if tool_calls >= MAX_TOOL_CALLS:
            return job, f"Reached the limit of {MAX_TOOL_CALLS} tool calls without finish."
        messages.append({"role": "user", "content": results})


def main():
    parser = argparse.ArgumentParser(description="Let Claude coordinate an organization's "
                                                 "specialists for one supplier email.")
    specialists.add_org_argument(parser)
    parser.add_argument("--email", required=True, type=Path, help="path of one supplier email")
    parser.add_argument("--save", type=Path, help="also write everything printed to this text file (UTF-8)")
    args = parser.parse_args()
    if not args.email.is_file():
        sys.exit(f"No email file at {args.email}")
    try:
        specialists.set_org(args.org)
    except SpecialistError as exc:
        sys.exit(str(exc))

    save_file = None
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        save_file = args.save.open("w", encoding="utf-8")
        sys.stdout = Tee(sys.__stdout__, save_file)
    try:
        job, stopped = run(args.email)
        summary(job.brief, job.results, stopped)
        print("\nOrchestrator's summary for a person:")
        if job.finished:
            text, decision_needed = job.finished
            print(f"  {text}")
            print(f"  A person must make a decision: {'YES' if decision_needed else 'no'}")
        else:
            print("  (none - the orchestrator did not call finish)")
    finally:
        if save_file:
            sys.stdout = sys.__stdout__
            save_file.close()
            print(f"\nSaved the output to {args.save}")


if __name__ == "__main__":
    main()
