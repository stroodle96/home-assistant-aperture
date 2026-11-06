"""Provider protocol adapters. Messages use the Chat Completions shape internally."""

import json
from collections.abc import AsyncIterable, AsyncIterator
from copy import deepcopy
from typing import Any

PROTOCOLS = ("chat_completions", "responses", "anthropic")


def restore_optional_fields(data: Any, schema: dict) -> Any:
    """Undo synthetic optional nulls introduced by strict output requirements."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)

    def visit(value, node):
        if not isinstance(node, dict):
            return value
        if "$ref" in node and node["$ref"].startswith("#/"):
            target = schema
            for segment in node["$ref"][2:].split("/"):
                target = target[segment.replace("~1", "/").replace("~0", "~")]
            return visit(value, target)
        for key in ("anyOf", "oneOf"):
            if key in node:
                for branch in node[key]:
                    candidate = visit(value, branch)
                    if validator.evolve(schema=branch).is_valid(candidate):
                        return candidate
        if isinstance(value, dict) and "properties" in node:
            properties = node["properties"]
            required = node.get("required", [])
            return {
                key: visit(item, properties.get(key, {}))
                for key, item in value.items()
                if not (
                    item is None
                    and key in properties
                    and key not in required
                    and not validator.evolve(schema=properties[key]).is_valid(None)
                )
            }
        if isinstance(value, list) and isinstance(node.get("items"), dict):
            return [visit(item, node["items"]) for item in value]
        return value

    return visit(data, schema)


def strict_schema(schema: dict) -> dict:
    """Make object schemas acceptable to providers' strict JSON output mode."""
    schema = deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                properties = node.setdefault("properties", {})
                required = node.get("required", [])
                for key, value in properties.items():
                    if key not in required:
                        properties[key] = {"anyOf": [value, {"type": "null"}]}
                node["required"] = list(properties)
                node["additionalProperties"] = False
            # Recurse only into JSON Schema positions. Property names and
            # example/default data can themselves contain schema keywords.
            for key in (
                "properties",
                "patternProperties",
                "$defs",
                "definitions",
                "dependentSchemas",
            ):
                for value in node.get(key, {}).values():
                    visit(value)
            for key in (
                "items",
                "additionalProperties",
                "unevaluatedProperties",
                "not",
                "if",
                "then",
                "else",
                "contains",
                "propertyNames",
            ):
                if isinstance(node.get(key), dict):
                    visit(node[key])
            for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
                for value in node.get(key, []):
                    visit(value)

    visit(schema)
    return schema


def build_request(
    protocol: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    *,
    schema: dict | None = None,
    temperature: float | None = None,
) -> tuple[str, dict]:
    """Build a model request without mutating messages or tool schemas."""
    if protocol not in PROTOCOLS:
        raise ValueError("Unsupported model API format")
    body: dict[str, Any] = {"model": model, "stream": True}
    if temperature is not None:
        body["temperature"] = temperature
    if protocol == "chat_completions":
        body.update(
            messages=[
                {
                    k: deepcopy(v)
                    for k, v in m.items()
                    if k in ("role", "content", "tool_calls", "tool_call_id")
                }
                for m in messages
            ],
            max_completion_tokens=max_tokens,
        )
        if tools:
            body["tools"] = [
                {"type": "function", "function": {**t, "strict": False}} for t in tools
            ]
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": strict_schema(schema),
                },
            }
        return "chat/completions", body
    if protocol == "responses":
        items = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message["tool_call_id"],
                        "output": message["content"],
                    }
                )
                continue
            # A reasoning item must precede the assistant output it describes.
            items.extend(message.get("reasoning_items", []))
            if content := message.get("content"):
                if isinstance(content, list):
                    content = [
                        {"type": "input_text", "text": p["text"]}
                        if p["type"] == "text"
                        else {"type": "input_image", "image_url": p["image_url"]["url"]}
                        for p in content
                    ]
                items.append({"role": role, "content": content})
            for tool in message.get("tool_calls", []):
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tool["id"],
                        "name": tool["function"]["name"],
                        "arguments": tool["function"]["arguments"],
                    }
                )
        body.update(
            input=items,
            max_output_tokens=max_tokens,
            store=False,
            include=["reasoning.encrypted_content"],
        )
        if tools:
            body["tools"] = [{"type": "function", **t, "strict": False} for t in tools]
        if schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "response",
                    "strict": True,
                    "schema": strict_schema(schema),
                }
            }
        return "responses", body
    body.update(max_tokens=max_tokens, messages=[])
    system = []
    for message in messages:
        role = message["role"]
        if role == "system":
            system.append(message.get("content") or "")
            continue
        blocks = []
        if role == "tool":
            role = "user"
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message["content"],
                }
            )
        else:
            content = message.get("content")
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if part["type"] == "text":
                        blocks.append(part)
                    else:
                        url = part["image_url"]["url"]
                        if url.startswith("data:"):
                            media, data = url[5:].split(";base64,", 1)
                            blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media,
                                        "data": data,
                                    },
                                }
                            )
                        else:
                            blocks.append(
                                {"type": "image", "source": {"type": "url", "url": url}}
                            )
            for tool in message.get("tool_calls", []):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool["id"],
                        "name": tool["function"]["name"],
                        "input": json.loads(tool["function"]["arguments"]),
                    }
                )
        if blocks:
            if body["messages"] and body["messages"][-1]["role"] == role:
                body["messages"][-1]["content"].extend(blocks)
            else:
                body["messages"].append({"role": role, "content": blocks})
    if system:
        body["system"] = "\n\n".join(system)
    if tools:
        body["tools"] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t["parameters"],
            }
            for t in tools
        ]
    if schema is not None:
        body["output_config"] = {
            "format": {"type": "json_schema", "schema": strict_schema(schema)}
        }
    return "messages", body


def _call(call: dict) -> dict:
    try:
        arguments = json.loads(call.get("arguments") or "{}")
    except json.JSONDecodeError as err:
        raise ValueError("Model returned invalid tool arguments") from err
    if not isinstance(arguments, dict) or not call.get("name") or not call.get("id"):
        raise ValueError("Model returned an invalid tool call")
    return {"id": call["id"], "name": call["name"], "arguments": arguments}


async def transform_stream(
    protocol: str, events: AsyncIterable[dict]
) -> AsyncIterator[dict]:
    """Normalize streaming text and complete tool calls; fail on partial output."""
    yield {"role": "assistant"}
    calls: dict[int, dict] = {}
    ready_calls: list[dict] = []
    completed = False
    meaningful = False
    async for event in events:
        if event.get("error") or event.get("type") == "error":
            raise ValueError("Model provider returned a streaming error")
        if protocol == "chat_completions":
            choices = event.get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta", {})
            if text := delta.get("content") or delta.get("refusal"):
                meaningful = True
                yield {"content": text}
            for fragment in delta.get("tool_calls") or []:
                call = calls.setdefault(
                    fragment["index"], {"id": "", "name": "", "arguments": ""}
                )
                if fragment.get("id"):
                    call["id"] = fragment["id"]
                function = fragment.get("function") or {}
                call["name"] += function.get("name") or ""
                call["arguments"] += function.get("arguments") or ""
            if reason := choice.get("finish_reason"):
                if reason not in ("stop", "tool_calls"):
                    raise ValueError("Model output was truncated or reached a limit")
                completed = True
                if calls:
                    meaningful = True
                    ready_calls.extend(_call(calls[i]) for i in sorted(calls))
                    calls.clear()
        elif protocol == "responses":
            kind = event.get("type")
            if kind in ("response.output_text.delta", "response.refusal.delta"):
                meaningful = True
                yield {"content": event["delta"]}
            elif kind == "response.output_item.done":
                item = event["item"]
                if item["type"] == "function_call":
                    meaningful = True
                    ready_calls.append(_call({**item, "id": item["call_id"]}))
                elif item["type"] == "reasoning":
                    yield {"reasoning_item": item}
            elif kind == "response.completed":
                completed = (
                    event.get("response", {}).get("status", "completed") == "completed"
                )
            elif kind in ("response.failed", "response.incomplete"):
                raise ValueError("Model response was incomplete or failed")
        elif protocol == "anthropic":
            kind = event.get("type")
            if (
                kind == "content_block_start"
                and event["content_block"]["type"] == "tool_use"
            ):
                block = event["content_block"]
                calls[event["index"]] = {
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": "",
                    "input": block.get("input", {}),
                }
            elif kind == "content_block_delta":
                delta = event["delta"]
                if delta["type"] == "text_delta":
                    meaningful = True
                    yield {"content": delta["text"]}
                elif delta["type"] == "input_json_delta":
                    calls[event["index"]]["arguments"] += delta["partial_json"]
            elif kind == "content_block_stop" and event["index"] in calls:
                call = calls.pop(event["index"])
                if not call["arguments"]:
                    call["arguments"] = json.dumps(call["input"])
                meaningful = True
                ready_calls.append(_call(call))
            elif kind == "message_delta" and event.get("delta", {}).get(
                "stop_reason"
            ) in ("max_tokens", "model_context_window_exceeded"):
                raise ValueError("Model output reached a token limit")
            elif kind == "message_stop":
                completed = True
        else:
            raise ValueError("Unsupported model API format")
    if not completed or calls or not meaningful:
        raise ValueError("Model stream ended empty or incomplete")
    if len({call["id"] for call in ready_calls}) != len(ready_calls):
        raise ValueError("Model returned duplicate tool call identifiers")
    # ChatLog schedules tools as soon as it receives this delta. Do not expose
    # actions until the complete provider stream has been accepted.
    if ready_calls:
        yield {"tool_calls": ready_calls}
