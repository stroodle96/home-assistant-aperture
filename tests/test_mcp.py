"""Independent MCP discovery and invocation against a real HTTP endpoint."""

import traceback

import httpx2
import pytest
from aiohttp import web

from custom_components.aperture.client import ApertureError
from custom_components.aperture.mcp_client import ApertureMCPClient, safe_tool_name


def test_tool_names_are_safe_stable_unique_and_bounded():
    first = safe_tool_name("gateway-a", "remote.tool/" + "long" * 30)
    assert len(first) <= 64
    assert first == safe_tool_name("gateway-a", "remote.tool/" + "long" * 30)
    assert first != safe_tool_name("gateway-b", "remote.tool/" + "long" * 30)
    assert safe_tool_name("a", "x-y") != safe_tool_name("a", "x/y")
    assert all(c.isalnum() or c == "_" for c in first)


async def test_paginated_discovery_and_call_preserve_remote_names(server):
    methods = []

    async def handler(request):
        if request.method != "POST":
            return web.Response(status=405)
        body = await request.json()
        methods.append(body["method"])
        method = body["method"]
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            cursor = body.get("params", {}).get("cursor")
            result = {
                "tools": [
                    {
                        "name": "second" if cursor else "remote.find",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
            if not cursor:
                result["nextCursor"] = "page-2"
        elif method == "tools/call":
            assert body["params"]["name"] == "remote.find"
            result = {"content": [{"type": "text", "text": "found"}], "isError": False}
        else:
            raise AssertionError(method)
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})

    async with httpx2.AsyncClient() as http:
        client = ApertureMCPClient(await server(handler), http, timeout=10)
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["remote.find", "second"]
        assert methods.count("tools/list") == 2
        result = await client.call_tool("remote.find", {})
        assert result["content"][0]["text"] == "found"
    # The SDK may discover an output schema while validating a tool result.
    assert methods.count("tools/call") == 1


async def test_failed_mcp_call_is_never_replayed(server):
    calls = []

    async def handler(request):
        if request.method != "POST":
            return web.Response(status=405)
        body = await request.json()
        if body["method"] == "initialize":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    },
                }
            )
        if body["method"] == "notifications/initialized":
            return web.Response(status=202)
        calls.append(body)
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32603, "message": "private upstream message"},
            }
        )

    async with httpx2.AsyncClient() as http:
        client = ApertureMCPClient(await server(handler), http, timeout=10)
        with pytest.raises(ApertureError) as error:
            await client.call_tool("write_tool", {})
    assert len(calls) == 1
    assert "private" not in str(error.value)
    assert "private upstream message" not in "".join(
        traceback.format_exception(error.value)
    )


async def test_mcp_auth_status_is_preserved(server):
    async def handler(request):
        return web.json_response({"error": "private"}, status=401)

    async with httpx2.AsyncClient() as http:
        client = ApertureMCPClient(await server(handler), http, timeout=5)
        with pytest.raises(ApertureError) as error:
            await client.list_tools()
    assert error.value.status == 401
