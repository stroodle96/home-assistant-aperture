"""End-to-end smoke test executed inside the official Home Assistant container.

Uses only a loopback fixture gateway. No account, paid model or home is accessed.
"""

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from aiohttp import web

logging.basicConfig(level=logging.WARNING)


def prepare_config(directory):
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "custom_components",
        Path(directory) / "custom_components",
    )
    (Path(directory) / "configuration.yaml").write_text(
        "homeassistant:\n  name: Fixture\nhttp:\n  server_host: 127.0.0.1\nconversation:\n",
        encoding="utf-8",
    )


async def main():
    from homeassistant.bootstrap import async_setup_hass
    from homeassistant.helpers import llm
    from homeassistant.runner import RuntimeConfig

    calls = []
    requests = []
    unsafe_requests = []

    async def gateway(request):
        if request.path.startswith("/different"):
            unsafe_requests.append(request.headers.get("Authorization"))
            return web.json_response({"data": [{"id": "fixture/model"}]})
        if request.path == "/v1/models":
            return web.json_response(
                {"data": [{"id": "fixture/model", "owned_by": "fixture"}]}
            )
        if request.path == "/v1/mcp":
            if request.method != "POST":
                return web.Response(status=405)
            body = await request.json()
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
                result = {
                    "tools": [
                        {
                            "name": "fixture_lookup",
                            "description": "Look up the fixture value",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"key": {"type": "string"}},
                                "required": ["key"],
                            },
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                }
                result["tools"].append(
                    {
                        "name": "unsupported_schema",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"a": {"type": "string"}},
                            "dependentRequired": {"a": ["b"]},
                        },
                    }
                )
            elif method == "tools/call":
                calls.append(body["params"])
                result = {
                    "content": [{"type": "text", "text": "The fixture value is 42."}],
                    "isError": False,
                }
            else:
                raise AssertionError(method)
            return web.json_response(
                {"jsonrpc": "2.0", "id": body["id"], "result": result}
            )
        if request.path == "/v1/chat/completions":
            body = await request.json()
            requests.append(body)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            if "response_format" in body:
                delta = {"content": '{"answer":42}'}
                reason = "stop"
            elif body.get("tools") and not any(
                m["role"] == "tool" for m in body["messages"]
            ):
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_fixture",
                            "function": {
                                "name": body["tools"][0]["function"]["name"],
                                "arguments": '{"key":"answer"}',
                            },
                        }
                    ]
                }
                reason = "tool_calls"
            else:
                delta = {"content": "The answer is 42."}
                reason = "stop"
            await response.write(
                (
                    "data: "
                    + json.dumps(
                        {"choices": [{"delta": delta, "finish_reason": reason}]}
                    )
                    + "\n\ndata: [DONE]\n\n"
                ).encode()
            )
            return response
        raise AssertionError(request.path)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", gateway)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    temporary = tempfile.TemporaryDirectory()
    directory = temporary.name
    try:
        await asyncio.to_thread(prepare_config, directory)
        hass = await async_setup_hass(
            RuntimeConfig(config_dir=directory, skip_pip=True)
        )
        assert hass is not None
        try:
            from homeassistant.helpers import entity_registry as er

            flow = await hass.config_entries.flow.async_init(
                "aperture",
                context={"source": "user"},
                data={
                    "url": url,
                    "api_key": "fixture-credential",
                    "enable_mcp": True,
                    "timeout": 15,
                },
            )
            assert flow["type"] == "create_entry", flow
            entry = flow["result"]
            await hass.async_block_till_done()
            assert entry.state.value == "loaded", (entry.state, entry.reason)
            for source in ("reconfigure", "reauth"):
                edit = await hass.config_entries.flow.async_init(
                    "aperture",
                    context={"source": source, "entry_id": entry.entry_id},
                    data=dict(entry.data) if source == "reauth" else None,
                )
                assert edit["type"] == "form", edit
                edit = await hass.config_entries.flow.async_configure(
                    edit["flow_id"],
                    {"url": url + "/different", "enable_mcp": True, "timeout": 15},
                )
                assert not unsafe_requests, unsafe_requests
                assert edit["type"] == "abort" and edit["reason"] == "wrong_gateway", (
                    edit
                )
            api_id = f"aperture_{entry.entry_id}"
            assert api_id in {a.id for a in llm.async_get_apis(hass)}

            # The public subentry flows create both native entity types.
            for kind in ("conversation", "ai_task_data"):
                subflow = await hass.config_entries.subentries.async_init(
                    (entry.entry_id, kind), context={"source": "user"}
                )
                assert subflow["type"] == "form", subflow
                data = {
                    "model": "fixture/model",
                    "protocol": "chat_completions",
                    "max_tokens": 1000,
                }
                if kind == "conversation":
                    data["llm_hass_api"] = [api_id]
                subflow = await hass.config_entries.subentries.async_configure(
                    subflow["flow_id"], data
                )
                assert subflow["type"] == "create_entry", subflow
                await hass.async_block_till_done()
            registry = er.async_get(hass)
            entities = er.async_entries_for_config_entry(registry, entry.entry_id)
            conversation_id = next(
                e.entity_id for e in entities if e.domain == "conversation"
            )
            task_id = next(e.entity_id for e in entities if e.domain == "ai_task")
            answer = await hass.services.async_call(
                "conversation",
                "process",
                {"agent_id": conversation_id, "text": "Look up the answer."},
                blocking=True,
                return_response=True,
            )
            assert "42" in str(answer), answer
            assert len(calls) == 1, calls
            assert calls[0]["name"] == "fixture_lookup"
            assert any(m["role"] == "tool" for m in requests[-1]["messages"])
            task_answer = await hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "entity_id": task_id,
                    "task_name": "Fixture",
                    "instructions": "Return the answer",
                    "structure": {
                        "answer": {"required": True, "selector": {"number": {}}}
                    },
                },
                blocking=True,
                return_response=True,
            )
            assert task_answer["data"]["answer"] == 42, task_answer
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert api_id not in {a.id for a in llm.async_get_apis(hass)}
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert api_id in {a.id for a in llm.async_get_apis(hass)}
            print(
                "PASS: real HA config flow, independent MCP discovery, model subentries, Assist tool round trip, structured AI task, unload and reload"
            )
        finally:
            await hass.async_stop()
    finally:
        await asyncio.to_thread(temporary.cleanup)
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
