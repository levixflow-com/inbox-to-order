"""Demo ERP MCP server (stdio): the fictional order system the app reads from and writes to.

Read tools: customers, catalog and orders. Write tools: `create_order` and `update_order`. The app
calls the write tools only after an operator clicks "Confirm"; the language model is never offered
them (see mcp_client.MODEL_ALLOWED_TOOLS). Every write is validated here again, is idempotent per
key and uses an optimistic version check.

To use a real ERP, write a server with the same tools that talks to that system (docs/EXTENDING.md).

Run standalone:  uv run python -m inbox_to_order.mcp_servers.erp_server
"""

from __future__ import annotations

import sqlite3

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..config import load_settings
from ..models import (
    CreateOrderRequest,
    CustomerLookup,
    OrderList,
    OrderLookup,
    OrderWriteResult,
    ProductList,
    ProductLookup,
    UpdateOrderRequest,
)
from ..services import erp

server = MCPServer(
    "demo-erp",
    instructions="Fictional Demo ERP: customers, catalog and orders. Writes require an operator's confirmation in the app.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _run(fn, *args):  # noqa: ANN001, ANN002, ANN202
    try:
        conn = erp.open_erp(load_settings().erp_db_path)
    except erp.ErpError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    try:
        return fn(conn, *args)
    except erp.ErpError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    except sqlite3.Error as exc:
        raise ToolError(f"DATABASE_ERROR: {type(exc).__name__}") from exc
    finally:
        conn.close()


@server.tool(
    description=(
        "Look up a customer by sender email address (exact address, then domain) or by exact legal name. "
        "Returns the customer and its ship-to addresses, or status 'not_found'. Never guesses."
    ),
    annotations=READ_ONLY,
)
def find_customer(sender_or_name: str) -> CustomerLookup:
    return _run(erp.find_customer, sender_or_name)


@server.tool(description="Get a customer and its ship-to addresses by customer_id.", annotations=READ_ONLY)
def get_customer(customer_id: str) -> CustomerLookup:
    return _run(erp.get_customer, customer_id)


@server.tool(
    description=(
        "Find catalog products for a customer by the customer's own item code, an internal SKU, or description "
        "words. Returns matched / ambiguous / not_found with candidates; ambiguous results must be resolved by a "
        "person, never by guessing."
    ),
    annotations=READ_ONLY,
)
def find_product(customer_id: str, customer_code_or_description: str) -> ProductLookup:
    return _run(erp.find_product, customer_id, customer_code_or_description)


@server.tool(description="List the catalog for a customer, with the customer's own item codes.", annotations=READ_ONLY)
def list_products(customer_id: str) -> ProductList:
    return _run(erp.list_products, customer_id)


@server.tool(
    description="Get the order for a customer and PO number from the Demo ERP (lines, ship-to, version), if it exists.",
    annotations=READ_ONLY,
)
def get_order(customer_id: str, po_number: str) -> OrderLookup:
    return _run(erp.get_order, customer_id, po_number)


@server.tool(description="List the most recently changed orders in the Demo ERP.", annotations=READ_ONLY)
def list_orders(limit: int = 20) -> OrderList:
    return _run(erp.list_orders, limit)


@server.tool(
    description=(
        "Create an order in the Demo ERP. Validates customer, ship-to ownership, SKUs, selling units and quantities; "
        "one order per customer and PO. Idempotent: the same idempotency_key never creates a second order."
    ),
    annotations=WRITE,
)
def create_order(request: CreateOrderRequest) -> OrderWriteResult:
    return _run(erp.create_order, request)


@server.tool(
    description=(
        "Replace the lines and ship-to of an existing Demo ERP order. Requires expected_version (fails with "
        "VERSION_CONFLICT if the order changed meanwhile). Idempotent per idempotency_key."
    ),
    annotations=WRITE,
)
def update_order(request: UpdateOrderRequest) -> OrderWriteResult:
    return _run(erp.update_order, request)


def main() -> None:
    server.run()  # stdio transport


if __name__ == "__main__":
    main()
