"""MCP server with Brookfield Bicycle Company's supply tools.

Tools
    get_part(part_id)                  stock on hand, scheduled shipments, models that use it
    get_orders_for_part(part_id)       open orders needing the part: units needed, ship day,
                                       late penalty
    check_coverage(part_id, arrivals)  each order covered, or units short and the first day
                                       it can ship, if arrivals replace the part's
                                       scheduled shipments

The data comes from week-05/brookfield-bikes/*.csv, read on every call. Days
are counted from today (day 0). The calculations live in supply_tools.py.

The server speaks MCP over stdio, so an AI app (Claude Code, or the agents in
this folder) starts it as a subprocess. From the top of the repository:

    claude mcp add brookfield-supply -- .venv\\Scripts\\python.exe week-05\\supply_mcp_server.py

stdout carries the MCP protocol, so status messages go to stderr.
"""

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


server = MCPServer(
    "brookfield-supply",
    instructions="Brookfield Bicycle Company's parts, scheduled shipments and open customer "
                 "orders. Days are counted from today (day 0). Report only numbers these "
                 "tools return.",
)


def run(tool, *args):
    try:
        return tool(*args)
    except supply_tools.SupplyError as exc:
        raise ToolError(str(exc)) from None


@server.tool(description=(
    "Get a part's description, supplier, stock on hand, scheduled shipments (purchase "
    "order, quantity, arrival day counted from today) and the bike models that use it. "
    "Part IDs look like MC-500."))
def get_part(part_id: str) -> dict:
    return run(supply_tools.get_part, part_id)


@server.tool(description=(
    "Get the open customer orders that need a part, earliest ship day first. For each: "
    "order ID, customer, model, bikes, parts per bike, units needed (bikes times parts "
    "per bike), ship day (counted from today) and whether a late penalty applies."))
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


def main():
    print(f"brookfield-supply serving data from {supply_tools.DATA_DIR}", file=sys.stderr, flush=True)
    server.run("stdio")


if __name__ == "__main__":
    main()
