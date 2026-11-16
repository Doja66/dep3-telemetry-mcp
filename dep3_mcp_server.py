#!/usr/bin/env python3
"""MCP server for the Department 3 Base telemetry feed.

Model Context Protocol over stdio: one JSON-RPC 2.0 message per line on stdin,
one reply per line on stdout. Diagnostics go to stderr, because stdout is the
protocol channel and a stray `print` there corrupts the session.

Standard library only, by design: a developer should be able to copy this file
next to their client config and run it with whatever `python` they already have.

Tools
  get_base_telemetry   Base block + three WETH/USDC pools   (0.005 USDC per call)
  get_base_whale_flow  USDC transfers of 25k USD or more    (0.010 USDC per call)
  get_base_arb_spreads cross-DEX spreads after fees and gas (0.015 USDC per call)
  get_payment_options  prices, address, network, checkout link (free)
  get_balance          remaining prepaid credit             (free)

Configuration (environment)
  DEP3_BASE_URL    override the service host (default: the live Worker)
  DEP3_CREDIT_KEY  prepaid credit key, `ck_...`, for the card rail
  DEP3_RECEIPT     an x402 settlement hash, for the crypto rail
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

SERVER_NAME = "dep3-telemetry"
SERVER_VERSION = "2.0.0"
DEFAULT_BASE_URL = "https://x402-dep3-edge.702digital66.workers.dev"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL = "2024-11-05"
HTTP_TIMEOUT_SECONDS = 45.0

INSTRUCTIONS = (
    "Department 3 sells three JSON feeds from Base mainnet: block and pool telemetry "
    "(0.005 USD), USDC whale flow (0.010 USD) and cross-DEX arbitrage spreads (0.015 USD). "
    "Each call settles on Base, either per call over x402 or against prepaid credit bought "
    "by card. Call get_payment_options first if you are unsure how to pay."
)

#: Shared by every paid tool: the same three ways to pay, the same envelope back.
PAID_PROPERTIES: dict[str, Any] = {
    "credit_key": {
        "type": "string",
        "description": "Prepaid credit key (ck_...). Defaults to DEP3_CREDIT_KEY.",
    },
    "receipt": {
        "type": "string",
        "description": "Base transaction hash that paid this call's price. "
        "Defaults to DEP3_RECEIPT.",
    },
    "idempotency_key": {
        "type": "string",
        "description": "Send the same value on a retry so the call is not charged twice.",
    },
}


def paid_tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {**PAID_PROPERTIES, **(properties or {})},
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    paid_tool(
        "get_base_telemetry",
        "Latest Base mainnet block and three WETH/USDC pools. Costs 0.005 USD per call. "
        "A standard x402 client pays with PAYMENT-SIGNATURE (EIP-3009); a manual buyer pays "
        "with a transfer hash, or with a prepaid credit key.",
        {
            "fields": {
                "type": "string",
                "description": "Comma-separated keys to return, e.g. block,pools.",
            }
        },
    ),
    paid_tool(
        "get_base_whale_flow",
        "USDC transfers of 25,000 USD or more on Base, decoded to sender, recipient and "
        "amount. Costs 0.010 USD per call.",
        {
            "min_usd": {
                "type": "number",
                "description": "Only return transfers at least this large. The feed starts "
                "at 25,000, so this can only raise the floor.",
            }
        },
    ),
    paid_tool(
        "get_base_arb_spreads",
        "WETH/USDC price spreads across two Uniswap v3 fee tiers and Aerodrome Slipstream, "
        "with both swap fees and gas subtracted, plus the buy/sell direction. Costs 0.015 USD "
        "per call.",
        {
            "min_net_bps": {
                "type": "number",
                "description": "Only return opportunities whose net spread is at least this "
                "many basis points.",
            }
        },
    ),
    {
        "name": "get_payment_options",
        "description": (
            "Return the live prices, pay-to address, network, asset, the 402 challenge header, "
            "and the Stripe checkout link for the $50 credit pack. Free: costs nothing."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_balance",
        "description": "Return the remaining prepaid credit and calls left for a credit key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "credit_key": {
                    "type": "string",
                    "description": "Prepaid credit key (ck_...). Defaults to DEP3_CREDIT_KEY.",
                }
            },
            "additionalProperties": False,
        },
    },
]


# --------------------------------------------------------------- transport --
def base_url() -> str:
    return os.getenv("DEP3_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL


def _header(headers: Any, name: str) -> str:
    """Case-insensitive header lookup that works for http.client and for dicts."""
    if not headers:
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def http_get(path: str, headers: dict[str, str]) -> tuple[int, Any, str]:
    """GET a JSON path. A 402 or 500 is a normal answer here, not an exception."""
    request = urllib.request.Request(
        base_url() + path,
        headers={
            "accept": "application/json",
            "user-agent": f"{SERVER_NAME}/{SERVER_VERSION}",
            **headers,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.headers, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", "replace")
        except OSError:
            body = ""
        return error.code, error.headers, body


def auth_headers(
    credit_key: str | None = None,
    receipt: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    key = (credit_key or os.getenv("DEP3_CREDIT_KEY", "")).strip()
    tx = (receipt or os.getenv("DEP3_RECEIPT", "")).strip()
    if key:
        headers["authorization"] = f"Bearer {key}"
    if tx:
        headers["x-x402-payment-receipt"] = tx
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    return headers


# -------------------------------------------------------------------- tools --
def text_result(body: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def payment_required_text(
    status: int, headers: Any, body: str, path: str = "/telemetry", price_usd: str = "0.005"
) -> str:
    challenge = _header(headers, "x-x402-payment-required")
    lines = [
        f"Payment required (HTTP {status}) for {path} at {price_usd} USD.",
        f"Challenge: {challenge or '(no challenge header returned)'}",
        "",
        "Two rails, pick one:",
        "  1. x402: a standard x402 client signs an EIP-3009 transferWithAuthorization",
        "     and sends it as PAYMENT-SIGNATURE. By hand: send the stated USDC amount",
        "     on Base, then retry with the argument receipt=<tx_hash>.",
        "  2. Card: buy the credit pack, then set DEP3_CREDIT_KEY to the key Stripe shows",
        "     you after payment.",
        "",
        "Run get_payment_options for the live prices, address and checkout link.",
    ]
    if body.strip():
        lines += ["", "Server response:", body.strip()]
    return "\n".join(lines)


def paid_request(
    path: str,
    price_usd: str,
    args: dict[str, Any],
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One paid GET: build the credential headers, call, and explain a 402."""
    headers = auth_headers(
        credit_key=args.get("credit_key"),
        receipt=args.get("receipt"),
        idempotency_key=args.get("idempotency_key"),
    )
    suffix = ""
    for key, value in (query or {}).items():
        if value is None or value == "":
            continue
        suffix += ("&" if suffix else "?") + f"{key}={value}"
    status, response_headers, body = http_get(path + suffix, headers)
    if status == 200:
        return text_result(body)
    if status == 402:
        return text_result(
            payment_required_text(status, response_headers, body, path, price_usd),
            is_error=True,
        )
    return text_result(f"{path} failed: HTTP {status}.\n{body.strip()}".strip(), is_error=True)


def tool_get_base_telemetry(args: dict[str, Any]) -> dict[str, Any]:
    return paid_request("/telemetry", "0.005", args, {"fields": args.get("fields")})


def tool_get_base_whale_flow(args: dict[str, Any]) -> dict[str, Any]:
    return paid_request("/whale-flow", "0.010", args, {"min_usd": args.get("min_usd")})


def tool_get_base_arb_spreads(args: dict[str, Any]) -> dict[str, Any]:
    return paid_request("/arb-spreads", "0.015", args, {"min_net_bps": args.get("min_net_bps")})


def tool_get_payment_options(_args: dict[str, Any]) -> dict[str, Any]:
    status, _, body = http_get("/.well-known/agent.json", {})
    if status != 200:
        return text_result(f"Could not read the discovery file: HTTP {status}.\n{body.strip()}",
                           is_error=True)
    try:
        card = json.loads(body)
    except json.JSONDecodeError:
        return text_result("The discovery file was not valid JSON.", is_error=True)

    resources = [item for item in (card.get("resources") or []) if isinstance(item, dict)]
    pack = card.get("credit_pack") or {}
    offers = []
    for resource in resources:
        path = str(resource.get("path") or "/telemetry")
        # The challenge header on the real resource is the authoritative value;
        # the card is documentation, this is what a client must actually send.
        probe_status, probe_headers, _ = http_get(path, {})
        challenge = (
            _header(probe_headers, "x-x402-payment-required") if probe_status == 402 else ""
        )
        offers.append(
            {
                "tool": {
                    "/telemetry": "get_base_telemetry",
                    "/whale-flow": "get_base_whale_flow",
                    "/arb-spreads": "get_base_arb_spreads",
                }.get(path, path),
                "path": path,
                "price_usd": resource.get("price_usd"),
                "amount_units": resource.get("amount_units"),
                "summary": resource.get("summary"),
                "challenge_header_value": challenge,
            }
        )

    first = resources[0] if resources else {}
    options = {
        "service": card.get("name"),
        "base_url": card.get("base_url") or base_url(),
        "network": first.get("network"),
        "asset": first.get("asset"),
        "asset_contract": first.get("asset_contract"),
        "pay_to": first.get("pay_to"),
        "rails": first.get("rails", []),
        "offers": offers,
        "crypto_rail": {
            "how": f"Sign an EIP-3009 transferWithAuthorization for the route's amount, "
                   f"send PAYMENT-SIGNATURE. Manual: send the USDC on "
                   f"{first.get('network')} to {first.get('pay_to')} and retry with "
                   f"X-x402-Payment-Receipt: <tx_hash>.",
            "then_retry_with_header": "X-x402-Payment-Receipt: <tx_hash>",
        },
        "card_rail": {
            "checkout": pack.get("checkout"),
            "price_usd": pack.get("price_usd"),
            "credit_micro_usd": pack.get("credit_micro_usd"),
            "calls_by_route": pack.get("calls_by_route"),
            "claim": pack.get("claim"),
            "then_set_env": "DEP3_CREDIT_KEY=<credit_key>",
        },
        "discovery": card.get("discovery", []),
    }
    return text_result(json.dumps(options, indent=2))


def tool_get_balance(args: dict[str, Any]) -> dict[str, Any]:
    key = (args.get("credit_key") or os.getenv("DEP3_CREDIT_KEY", "")).strip()
    if not key:
        return text_result(
            "No credit key. Pass credit_key, or set DEP3_CREDIT_KEY after buying the pack at "
            f"{base_url()}/llms.txt.",
            is_error=True,
        )
    status, _, body = http_get("/credits/balance", {"authorization": f"Bearer {key}"})
    if status == 200:
        return text_result(body)
    return text_result(
        f"Balance lookup failed: HTTP {status}.\n{body.strip()}".strip(), is_error=True
    )


HANDLERS = {
    "get_base_telemetry": tool_get_base_telemetry,
    "get_base_whale_flow": tool_get_base_whale_flow,
    "get_base_arb_spreads": tool_get_base_arb_spreads,
    "get_payment_options": tool_get_payment_options,
    "get_balance": tool_get_balance,
    # The v1 name, kept so an existing client config keeps working. It is not
    # advertised in tools/list.
    "get_telemetry": tool_get_base_telemetry,
}


# ----------------------------------------------------------------- protocol --
def rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: Any) -> dict[str, Any] | None:
    """Answer one message. Notifications (no id) get no reply, per JSON-RPC."""
    if not isinstance(message, dict):
        return rpc_error(None, -32600, "Invalid Request")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if request_id is None:
        # notifications/initialized, notifications/cancelled, and friends.
        return None

    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        negotiated = requested if requested in SUPPORTED_PROTOCOLS else FALLBACK_PROTOCOL
        return rpc_result(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return rpc_result(request_id, {})
    if method == "tools/list":
        return rpc_result(request_id, {"tools": TOOLS})
    if method in {"resources/list", "prompts/list"}:
        empty = "resources" if method == "resources/list" else "prompts"
        return rpc_result(request_id, {empty: []})
    if method == "tools/call":
        name = str(params.get("name") or "")
        handler = HANDLERS.get(name)
        if handler is None:
            return rpc_error(request_id, -32602, f"Unknown tool: {name or '(none)'}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return rpc_error(request_id, -32602, "Tool arguments must be an object")
        try:
            return rpc_result(request_id, handler(arguments))
        except Exception as error:  # noqa: BLE001 - a tool failure must not end the session
            return rpc_result(
                request_id,
                text_result(f"{type(error).__name__}: {error}", is_error=True),
            )

    return rpc_error(request_id, -32601, f"Method not found: {method}")


def serve() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    sys.stderr.write(f"{SERVER_NAME} {SERVER_VERSION} ready on {base_url()}\n")
    sys.stderr.flush()
    for raw in iter(stdin.readline, b""):
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError as error:
            reply: dict[str, Any] | None = rpc_error(None, -32700, f"Parse error: {error}")
        else:
            reply = handle(message)
        if reply is None:
            continue
        stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    if "--demo" in argv:
        # Prove the connection without speaking the protocol or spending anything.
        print(json.dumps(json.loads(tool_get_payment_options({})["content"][0]["text"]), indent=2))
        return 0
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
