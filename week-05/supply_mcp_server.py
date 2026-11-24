"""MCP server with an organization's supply tools.

Tools
    get_part(part_id)                  stock on hand, scheduled shipments, products that use it
    get_orders_for_part(part_id)       open orders needing the part: units needed, ship day,
                                       late penalty
    check_coverage(part_id, arrivals)  each order covered, or units short and the first day
                                       it can ship, if arrivals replace the part's
                                       scheduled shipments

--org picks the organization folder in week-05 (default brookfield-bikes); its CSV files
are read on every call. Days are counted from today (day 0). The calculations live in
supply_tools.py.

The server speaks MCP over stdio, so an AI app (Claude Code, or the agents in
this folder) starts it as a subprocess. From the top of the repository:

    claude mcp add brookfield-supply -- .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py
    claude mcp add copperline-supply -- .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py --org copperline-coffee

stdout carries the MCP protocol, so status messages go to stderr.
"""

import argparse
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supply_tools  # noqa: E402  (found beside this file, whatever the working folder)


class Arrival(BaseModel):
    qty: int = Field(gt=0, description="Number of units in this shipment.")
    arrives_in_days: int = Field(ge=0, description="Day the shipment arrives, counted from today (day 0).")


def run(tool, *args):
    try:
        return tool(*args)
    except supply_tools.SupplyError as exc:
        raise ToolError(str(exc)) from None


def build_server(org):
    server = MCPServer(
        f"{org}-supply",
        instructions=f"The parts, scheduled shipments and open customer orders of {org}. "
                     "Days are counted from today (day 0). Report only numbers these "
                     "tools return.",
    )

    @server.tool(description=(
        "Get a part's description, supplier, stock on hand, scheduled shipments (purchase "
        "order, quantity, arrival day counted from today) and the products that use it. "
        "Part IDs look like the ones in the supplier's email."))
    def get_part(part_id: str) -> dict:
        return run(supply_tools.get_part, part_id)

    @server.tool(description=(
        "Get the open customer orders that need a part, earliest ship day first. For each: "
        "order ID, customer, product, quantity, parts per unit, units needed (quantity times "
        "parts per unit), ship day (counted from today) and whether a late penalty applies."))
    def get_orders_for_part(part_id: str) -> dict:
        return run(supply_tools.get_orders_for_part, part_id)

    @server.tool(description=(
        "Check whether a new list of arrivals for a part covers its open orders. The arrivals "
        "REPLACE all of the part's scheduled shipments, so list every shipment still expected, "
        "each with its quantity and arrival day counted from today. Orders are filled complete "
        "in ship-day order, so a short order holds up the ones after it. Returns each order as "
        "covered, or short with units_short, ready_day (the first day it can ship; null if the "
        "arrivals never cover it), days_late and late_penalty."))
    def check_coverage(part_id: str, arrivals: list[Arrival]) -> dict:
        return run(supply_tools.check_coverage, part_id, [a.model_dump() for a in arrivals])

    return server


def main():
    parser = argparse.ArgumentParser(description="Serve an organization's supply tools over MCP (stdio).")
    parser.add_argument("--org", default=supply_tools.DEFAULT_ORG,
                        help=f"organization folder in week-05 (default {supply_tools.DEFAULT_ORG})")
    args = parser.parse_args()
    try:
        supply_tools.set_org(args.org)
        # Check the CSV headers now, so a data problem shows at start-up, not mid-run.
        for name in supply_tools.CSV_FILES:
            supply_tools.read_table(name)
    except supply_tools.SupplyError as exc:
        sys.exit(f"supply_mcp_server: {exc}")
    print(f"{args.org}-supply serving data from {supply_tools.DATA_DIR}", file=sys.stderr, flush=True)
    build_server(args.org).run("stdio")


if __name__ == "__main__":
    main()
