"""Provider payload and stream behavior, independent of Home Assistant."""

import json

import pytest

from custom_components.aperture.protocols import (
    build_request,
    strict_schema,
    transform_stream,
)


def test_structured_optional_nulls_are_restored_without_removing_nullable_fields():
    from custom_components.aperture.protocols import restore_optional_fields

    schema = {
        "type": "object",
        "properties": {
            "optional": {"type": "string"},
            "nullable": {"type": ["string", "null"]},
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"optional": {"type": "integer"}},
                },
            },
        },
        "required": ["rows"],
    }
    data = {"optional": None, "nullable": None, "rows": [{"optional": None}]}
    assert restore_optional_fields(data, schema) == {"nullable": None, "rows": [{}]}
    assert data["optional"] is None


def test_structured_field_named_properties_is_not_interpreted_as_schema():
    from jsonschema import Draft202012Validator

    schema = {
        "type": "object",
        "properties": {
            "properties": {"type": "string"},
            "metadata": {"type": "object", "properties": {"type": {"type": "string"}}},
        },
        "required": ["properties"],
    }
    result = strict_schema(schema)
    Draft202012Validator.check_schema(result)
    assert result["properties"]["properties"] == {"type": "string"}
    assert "metadata" not in schema["required"]


@pytest.mark.parametrize(
    "protocol,stream",
    [
        (
            "responses",
            [
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "write",
                        "arguments": "{}",
                    },
                },
                {"type": "response.incomplete"},
            ],
        ),
        (
            "anthropic",
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "c1",
                        "name": "write",
                        "input": {},
                    },
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
            ],
        ),
        (
            "chat_completions",
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "function": {
                                            "name": "write",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                {"error": {"message": "broken stream"}},
            ],
        ),
    ],
)
async def test_failed_stream_never_exposes_tool_calls_to_executor(protocol, stream):
    received = []
    with pytest.raises(ValueError):
        async for value in transform_stream(protocol, events(*stream)):
            received.append(value)
    assert not any(value.get("tool_calls") for value in received)


async def events(*values):
    for value in values:
        yield value


def test_chat_request_keeps_qualified_model_and_optional_parameters_absent():
    path, body = build_request(
        "chat_completions",
        "provider/model",
        [{"role": "user", "content": "Hello"}],
        [],
        100,
    )
    assert path == "chat/completions"
    assert body["model"] == "provider/model"
    assert body["max_completion_tokens"] == 100
    assert "temperature" not in body
    assert "tools" not in body


@pytest.mark.parametrize(
    "protocol,key",
    [
        ("chat_completions", "response_format"),
        ("responses", "text"),
        ("anthropic", "output_config"),
    ],
)
def test_structured_output_protocol(protocol, key):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    _, body = build_request(
        protocol,
        "model",
        [{"role": "user", "content": "Hello"}],
        [],
        200,
        schema=schema,
    )
    assert key in body
    assert schema == json.loads(json.dumps(schema))


def test_responses_tool_history_preserves_call_ids():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "found"},
    ]
    _, body = build_request("responses", "model", messages, [], 200)
    assert body["store"] is False
    assert body["input"][0]["call_id"] == "call_1"
    assert body["input"][1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "found",
    }


def test_responses_reasoning_precedes_assistant_output_and_stays_protocol_specific():
    messages = [
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_items": [
                {"type": "reasoning", "id": "r1", "encrypted_content": "opaque"}
            ],
        }
    ]
    _, response_body = build_request("responses", "model", messages, [], 100)
    assert response_body["input"][0]["type"] == "reasoning"
    _, chat_body = build_request("chat_completions", "model", messages, [], 100)
    assert "reasoning_items" not in chat_body["messages"][0]


def test_anthropic_tool_results_become_user_blocks():
    _, body = build_request(
        "anthropic",
        "model",
        [
            {"role": "system", "content": "Instructions"},
            {"role": "tool", "tool_call_id": "t1", "content": "found"},
        ],
        [],
        200,
    )
    assert body["system"] == "Instructions"
    assert body["messages"][0]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "found",
    }


async def test_fragmented_chat_tool_arguments_are_emitted_once():
    stream = events(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call1",
                                "function": {"name": "find", "arguments": '{"q":'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"hello"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": []},
    )
    output = [x async for x in transform_stream("chat_completions", stream)]
    calls = [c for x in output for c in x.get("tool_calls", [])]
    assert calls == [{"id": "call1", "name": "find", "arguments": {"q": "hello"}}]


async def test_responses_function_call_and_text_stream():
    stream = events(
        {"type": "response.output_text.delta", "delta": "Hello"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "c1",
                "name": "find",
                "arguments": "{}",
            },
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    )
    output = [x async for x in transform_stream("responses", stream)]
    assert "".join(x.get("content", "") for x in output) == "Hello"
    assert [c for x in output for c in x.get("tool_calls", [])] == [
        {"id": "c1", "name": "find", "arguments": {}}
    ]


async def test_anthropic_fragmented_tools():
    stream = events(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "c1",
                "name": "find",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"q":"hi"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    )
    output = [x async for x in transform_stream("anthropic", stream)]
    assert [c for x in output for c in x.get("tool_calls", [])] == [
        {"id": "c1", "name": "find", "arguments": {"q": "hi"}}
    ]


@pytest.mark.parametrize(
    "protocol,stream",
    [
        (
            "chat_completions",
            [
                {
                    "choices": [
                        {"delta": {"content": "partial"}, "finish_reason": "length"}
                    ]
                }
            ],
        ),
        (
            "responses",
            [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
        ),
        (
            "anthropic",
            [{"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}],
        ),
    ],
)
async def test_truncated_outputs_raise(protocol, stream):
    with pytest.raises(ValueError, match="limit|incomplete|truncated"):
        _ = [x async for x in transform_stream(protocol, events(*stream))]


async def test_missing_terminal_event_raises():
    with pytest.raises(ValueError, match="ended|incomplete"):
        _ = [
            x
            async for x in transform_stream(
                "chat_completions",
                events({"choices": [{"delta": {"content": "partial"}}]}),
            )
        ]
