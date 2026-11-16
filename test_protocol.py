"""Protocol-level tests for the starter-kit MCP server.

The HTTP layer is stubbed, so these run offline and cost nothing. They check the
things a client will actually break on: the initialize handshake, the tool list,
the notification rule, and how a 402 is surfaced.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).with_name("dep3_mcp_server.py")
SPEC = importlib.util.spec_from_file_location("dep3_mcp_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

AGENT_CARD = {
    "name": "Department 3 Base telemetry",
    "base_url": "https://example.test",
    "resources": [
        {
            "url": "https://example.test/telemetry",
            "method": "GET",
            "price_usd": 0.005,
            "amount_units": "5000",
            "network": "eip155:8453",
            "asset": "USDC",
            "asset_contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "pay_to": "0x07Efb263C7218F82aD1e7304eB93bb1b3e7C104b",
            "rails": ["x402_base_usdc", "stripe_prepaid_credit"],
            "credit_pack": {
                "price_usd": 50,
                "calls": 10000,
                "checkout": "https://buy.stripe.com/test",
                "claim": "https://example.test/credits/claim?session_id=<id>",
            },
        }
    ],
    "discovery": ["https://example.test/llms.txt"],
}

CHALLENGE = "USDC:0.005@Base:0x07Efb263C7218F82aD1e7304eB93bb1b3e7C104b"


def call(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return server.handle(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    )


class ProtocolTests(unittest.TestCase):
    def test_initialize_echoes_a_supported_version(self):
        reply = call("initialize", {"protocolVersion": "2025-06-18"})
        assert reply is not None
        assert reply["result"]["protocolVersion"] == "2025-06-18"
        assert reply["result"]["capabilities"]["tools"] is not None
        assert reply["result"]["serverInfo"]["name"] == "dep3-telemetry"

    def test_initialize_falls_back_for_an_unknown_version(self):
        result = call("initialize", {"protocolVersion": "1999-01-01"})["result"]
        assert result["protocolVersion"] == server.FALLBACK_PROTOCOL

    def test_notifications_get_no_reply(self):
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_ping(self):
        assert call("ping")["result"] == {}

    def test_tools_list_describes_every_tool(self):
        tools = call("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        assert names == {"get_telemetry", "get_payment_options", "get_balance"}
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"
            assert tool["description"]

    def test_unknown_method_is_an_error(self):
        assert call("tools/nope")["error"]["code"] == -32601

    def test_unknown_tool_is_an_error(self):
        reply = call("tools/call", {"name": "rm_rf", "arguments": {}})
        assert reply["error"]["code"] == -32602

    def test_non_object_arguments_are_refused(self):
        reply = call("tools/call", {"name": "get_telemetry", "arguments": ["nope"]})
        assert reply["error"]["code"] == -32602

    def test_a_handler_crash_does_not_kill_the_session(self):
        with mock.patch.object(server, "http_get", side_effect=TimeoutError("slow")):
            reply = call("tools/call", {"name": "get_telemetry", "arguments": {}})
        assert reply["result"]["isError"] is True
        assert "TimeoutError" in reply["result"]["content"][0]["text"]


class TelemetryTests(unittest.TestCase):
    def test_paid_call_returns_the_body(self):
        with mock.patch.object(server, "http_get", return_value=(200, {}, '{"status":"ok"}')):
            reply = call("tools/call", {"name": "get_telemetry", "arguments": {}})
        assert reply["result"]["isError"] is False
        assert json.loads(reply["result"]["content"][0]["text"])["status"] == "ok"

    def test_unpaid_call_explains_the_402(self):
        headers = {"X-x402-Payment-Required": CHALLENGE}
        with mock.patch.object(server, "http_get", return_value=(402, headers, '{"error":"X"}')):
            reply = call("tools/call", {"name": "get_telemetry", "arguments": {}})
        text = reply["result"]["content"][0]["text"]
        assert reply["result"]["isError"] is True
        assert CHALLENGE in text
        assert "DEP3_CREDIT_KEY" in text

    def test_receipt_is_sent_as_the_payment_header(self):
        seen: dict = {}

        def fake(path, headers):
            seen.update(headers)
            return 200, {}, "{}"

        with mock.patch.object(server, "http_get", side_effect=fake):
            call("tools/call", {"name": "get_telemetry", "arguments": {"receipt": "0xabc"}})
        assert seen["x-x402-payment-receipt"] == "0xabc"

    def test_credit_key_becomes_a_bearer_header(self):
        seen: dict = {}

        def fake(path, headers):
            seen.update(headers)
            return 200, {}, "{}"

        with mock.patch.object(server, "http_get", side_effect=fake):
            call("tools/call", {"name": "get_telemetry", "arguments": {"credit_key": "ck_1"}})
        assert seen["authorization"] == "Bearer ck_1"

    def test_origin_failure_is_reported(self):
        with mock.patch.object(server, "http_get", return_value=(502, {}, '{"error":"down"}')):
            reply = call("tools/call", {"name": "get_telemetry", "arguments": {}})
        assert "502" in reply["result"]["content"][0]["text"]
        assert reply["result"]["isError"] is True


class PaymentAndBalanceTests(unittest.TestCase):
    def test_payment_options_combine_card_and_challenge(self):
        def fake(path, headers):
            if path == "/.well-known/agent.json":
                return 200, {}, json.dumps(AGENT_CARD)
            return 402, {"X-x402-Payment-Required": CHALLENGE}, "{}"

        with mock.patch.object(server, "http_get", side_effect=fake):
            reply = call("tools/call", {"name": "get_payment_options", "arguments": {}})
        options = json.loads(reply["result"]["content"][0]["text"])
        assert options["challenge_header_value"] == CHALLENGE
        assert options["card_rail"]["checkout"] == "https://buy.stripe.com/test"
        assert options["pay_to"].startswith("0x07Ef")

    def test_balance_without_a_key_explains_what_is_missing(self):
        with mock.patch.dict(server.os.environ, {}, clear=True):
            reply = call("tools/call", {"name": "get_balance", "arguments": {}})
        assert reply["result"]["isError"] is True
        assert "credit key" in reply["result"]["content"][0]["text"].lower()

    def test_balance_with_a_key_returns_the_ledger(self):
        with mock.patch.object(server, "http_get", return_value=(200, {}, '{"balance":5000}')):
            reply = call(
                "tools/call", {"name": "get_balance", "arguments": {"credit_key": "ck_1"}}
            )
        assert json.loads(reply["result"]["content"][0]["text"])["balance"] == 5000


if __name__ == "__main__":
    unittest.main()
