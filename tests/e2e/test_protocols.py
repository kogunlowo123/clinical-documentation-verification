"""A2A JSON-RPC and MCP streamable-HTTP surfaces, end to end through the ASGI app."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CLEAN_ENCOUNTER, agent_headers, clinician_headers
from tests.fakes import FakeAgentIssuer, FakeIdp

pytestmark = pytest.mark.e2e

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def rpc(
    client: TestClient, headers: dict[str, str], method: str, params: dict[str, Any], rid: Any = 1
) -> Any:
    response = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def a2a_message(parts: list[dict[str, Any]], **message: Any) -> dict[str, Any]:
    return {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": "msg-001",
            "parts": parts,
            **message,
        }
    }


class TestA2A:
    def test_message_send_with_data_part_then_get(
        self, client: TestClient, agent_issuer: FakeAgentIssuer
    ) -> None:
        headers = agent_headers(agent_issuer)
        data = {"patient_id": "pat-1001", "encounter_id": "enc-3001", "transcript": CLEAN_ENCOUNTER}
        result = rpc(
            client, headers, "message/send", a2a_message([{"kind": "data", "data": data}])
        )["result"]
        assert result["kind"] == "task"
        assert result["status"]["state"] == "input-required"
        assert result["contextId"] == "enc-3001"
        summary = result["artifacts"][0]["parts"][0]["data"]
        assert summary["band"] == "consistent"
        assert "PROGRESS NOTE" in result["artifacts"][1]["parts"][0]["text"]

        replay = rpc(client, headers, "message/send", a2a_message([{"kind": "data", "data": data}]))
        assert replay["result"]["id"] == result["id"]  # messageId is the idempotency key

        got = rpc(client, headers, "tasks/get", {"id": result["id"]}, rid="r2")
        assert got["id"] == "r2"
        assert got["result"]["id"] == result["id"]
        cancel = rpc(client, headers, "tasks/cancel", {"id": result["id"]})
        assert cancel["error"]["code"] == -32002

    def test_message_send_with_text_part_and_metadata(
        self, client: TestClient, clinician_idp: FakeIdp
    ) -> None:
        params = a2a_message(
            [{"kind": "text", "text": CLEAN_ENCOUNTER}],
            metadata={"patient_id": "pat-1001", "encounter_id": "enc-3002", "note_type": "11488-4"},
            messageId="msg-002",
        )
        result = rpc(client, clinician_headers(clinician_idp), "message/send", params)["result"]
        assert result["status"]["state"] == "input-required"

    def test_protocol_errors(self, client: TestClient, agent_issuer: FakeAgentIssuer) -> None:
        headers = agent_headers(agent_issuer)
        assert rpc(client, headers, "tasks/resubscribe", {})["error"]["code"] == -32601
        assert rpc(client, headers, "tasks/get", {})["error"]["code"] == -32602
        missing = rpc(client, headers, "tasks/get", {"id": "00000000-0000-0000-0000-000000000000"})
        assert missing["error"]["code"] == -32001
        bad = rpc(client, headers, "message/send", a2a_message([{"kind": "text", "text": "hello"}]))
        assert bad["error"]["code"] == -32602
        empty = rpc(client, headers, "message/send", {"message": {"role": "user", "parts": []}})
        assert empty["error"]["code"] == -32602
        not_user = rpc(
            client, headers, "message/send", {"message": {"role": "agent", "parts": [{}]}}
        )
        assert not_user["error"]["code"] == -32602
        no_text = rpc(client, headers, "message/send", a2a_message([{"kind": "file", "file": {}}]))
        assert no_text["error"]["code"] == -32602

        invalid = client.post("/a2a", json={"id": 7, "method": "message/send"}, headers=headers)
        assert invalid.json()["error"]["code"] == -32600
        not_object = client.post("/a2a", json=[1, 2], headers=headers)
        assert not_object.json()["error"]["code"] == -32600
        garbage = client.post(
            "/a2a", content=b"{not json", headers={**headers, "content-type": "application/json"}
        )
        assert garbage.json()["error"]["code"] == -32700

    def test_requires_authentication(self, client: TestClient) -> None:
        response = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_forbidden_agent_is_reported_over_http(
        self, client: TestClient, agent_issuer: FakeAgentIssuer
    ) -> None:
        headers = agent_headers(agent_issuer, scope="documentation:read")
        data = {"patient_id": "pat-1001", "encounter_id": "enc-3003", "transcript": CLEAN_ENCOUNTER}
        response = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": a2a_message([{"kind": "data", "data": data}]),
            },
            headers=headers,
        )
        assert response.status_code == 403


class TestMCP:
    def call(
        self, client: TestClient, headers: dict[str, str], method: str, params: dict[str, Any]
    ) -> Any:
        response = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={**MCP_HEADERS, **headers},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_tools_are_listed_with_schemas(self, client: TestClient) -> None:
        tools = self.call(client, {}, "tools/list", {})["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "verify_clinical_documentation",
            "get_verification",
            "validate_orders",
            "parse_medication_sig",
        }
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"
            assert tool["outputSchema"]["type"] == "object"

    def test_verify_and_get_through_mcp(
        self, client: TestClient, agent_issuer: FakeAgentIssuer
    ) -> None:
        headers = agent_headers(agent_issuer)
        request = {
            "patient_id": "pat-1001",
            "encounter_id": "enc-4001",
            "transcript": {"text": CLEAN_ENCOUNTER},
        }
        result = self.call(
            client,
            headers,
            "tools/call",
            {"name": "verify_clinical_documentation", "arguments": {"request": request}},
        )["result"]
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["awaiting_clinician_signoff"] is True
        vid = structured["summary"]["id"]
        got = self.call(
            client,
            headers,
            "tools/call",
            {"name": "get_verification", "arguments": {"request": {"verification_id": vid}}},
        )["result"]
        assert got["structuredContent"]["status"] == "pending_approval"
        metrics = client.get("/metrics").text
        assert (
            'cdv_mcp_tool_calls_total{outcome="success",tool="verify_clinical_documentation"}'
            in metrics
        )

    def test_order_tools(self, client: TestClient, clinician_idp: FakeIdp) -> None:
        headers = clinician_headers(clinician_idp, "physician")
        validated = self.call(
            client,
            headers,
            "tools/call",
            {
                "name": "validate_orders",
                "arguments": {
                    "request": {
                        "orders": [
                            {"text": "oxycodone 5 mg PO q6h prn pain"},
                            {"text": "lorazepam 1 mg PO qhs"},
                        ]
                    }
                },
            },
        )["result"]["structuredContent"]
        rules = {f["rule"] for f in validated["findings"]}
        assert {"drug-interaction", "controlled-substance"} <= rules
        parsed = self.call(
            client,
            headers,
            "tools/call",
            {
                "name": "parse_medication_sig",
                "arguments": {"request": {"text": "insulin glargine 20 units SC at bedtime"}},
            },
        )["result"]["structuredContent"]
        assert parsed["route"] == "SC"

    def test_unauthenticated_and_forbidden_calls_are_tool_errors(
        self, client: TestClient, agent_issuer: FakeAgentIssuer
    ) -> None:
        anonymous = self.call(
            client,
            {},
            "tools/call",
            {"name": "parse_medication_sig", "arguments": {"request": {"text": "aspirin 81 mg"}}},
        )["result"]
        assert anonymous["isError"] is True
        assert "unauthorized" in json.dumps(anonymous["content"])
        weak = agent_headers(agent_issuer, scope="documentation:read")
        denied = self.call(
            client,
            weak,
            "tools/call",
            {"name": "parse_medication_sig", "arguments": {"request": {"text": "aspirin 81 mg"}}},
        )["result"]
        assert denied["isError"] is True
        assert "forbidden" in json.dumps(denied["content"])
