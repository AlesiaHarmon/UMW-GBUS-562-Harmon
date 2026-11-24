"""A fixed pipeline for one supplier email, for one organization.

Runs the specialists from specialists.py in a fixed order:
    1. The supply analyst.
    2. A customer notice for each order at risk (skipped if there are none).
    3. A reply to the supplier.
Every draft goes through the review step (writer -> reviewer -> up to 2 revisions ->
name check) and is saved in week-05/drafts/<org>. At the end it prints a summary: drafts
saved, drafts marked for a person, decisions for a person, and total tokens.

--org picks the organization folder in week-05 (default brookfield-bikes). From the top
of the repository:
    .venv\\Scripts\\python.exe week-05\\pipeline.py --email week-05\\brookfield-bikes\\supplier-emails\\01-northgate-controller-delay.eml
    .venv\\Scripts\\python.exe week-05\\pipeline.py --org copperline-coffee --email week-05\\copperline-coffee\\supplier-emails\\01-danforth-pump-delay.eml
Add --save run.txt to also write everything printed to run.txt (UTF-8).
"""

import argparse
import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
import specialists  # noqa: E402  (found beside this file, whatever the working folder)
from specialists import SpecialistError  # noqa: E402
from supply_tools import SupplyError  # noqa: E402


class Tee:
    """Write to the terminal and to the --save file at the same time."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def step(number, title):
    print("\n" + "#" * 70)
    print(f"# STEP {number}: {title}")
    print("#" * 70)


def run(email_path):
    """The three steps for one email. Returns (brief or None, results, stop reason or None)."""
    email = email_path.read_text(encoding="utf-8")
    print(f"Organization: {specialists.ORG}")
    print(f"Supplier email: {email_path}")
    brief, results = None, []
    try:
        step(1, "supply analyst")
        brief = specialists.run_analyst(email)

        orders = brief.get("orders_at_risk", [])
        if orders:
            step(2, f"customer notices for {len(orders)} order(s) at risk")
            for order in orders:
                results.append(specialists.reviewed_customer_notice(order, brief))
        else:
            step(2, "customer notices - skipped, no orders at risk")

        step(3, "reply to the supplier")
        results.append(specialists.reviewed_supplier_reply(email, brief))
        return brief, results, None
    except (SpecialistError, SupplyError) as exc:
        return brief, results, str(exc)
    except anthropic.AuthenticationError:
        return brief, results, "Claude rejected the API key."
    except anthropic.RateLimitError:
        return brief, results, "Rate limit reached. Wait a moment and try again."
    except anthropic.APIStatusError as exc:
        return brief, results, f"Claude API error {exc.status_code}: {exc.message}"
    except anthropic.APIConnectionError:
        return brief, results, "Could not reach the Claude API. Check your internet connection."


def summary(brief, results, stopped):
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if stopped:
        print(f"Stopped early: {stopped}")

    print(f"\nDrafts saved: {len(results)}")
    for r in results:
        who = f"customer, order {r['order_id']}" if r["recipient"] == "customer" else "supplier"
        print(f"  - {Path(r['path']).name}  ({who}; reviewer {r['verdict']}, "
              f"{r['revisions']} revision(s))")

    marked = [r for r in results if r["needs_person"]]
    print(f"\nDrafts marked for a person: {len(marked)}")
    for r in marked:
        print(f"  - {Path(r['path']).name}")
        for reason in r["reasons"]:
            print(f"      {reason}")

    decisions = brief.get("decisions_for_a_person", []) if brief else []
    print(f"\nDecisions for a person: {len(decisions)}")
    for d in decisions:
        print(f"  - {d}")

    specialists.print_token_report()


def main():
    parser = argparse.ArgumentParser(description="Run an organization's specialists on one supplier email.")
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
        brief, results, stopped = run(args.email)
        summary(brief, results, stopped)
    finally:
        if save_file:
            sys.stdout = sys.__stdout__
            save_file.close()
            print(f"\nSaved the output to {args.save}")


if __name__ == "__main__":
    main()
