"""Real HTTP fixture tests for the Aperture model client."""

import pytest
from aiohttp import ClientSession, web

from custom_components.aperture.client import (
    ApertureClient,
    ApertureError,
    normalize_url,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://gateway.example/", "https://gateway.example/v1"),
        ("https://gateway.example/v1/", "https://gateway.example/v1"),
        ("http://localhost:8080/prefix", "http://localhost:8080/prefix/v1"),
    ],
)
def test_url_normalization(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gateway.example",
        "https://user:pass@gateway.example",
        "https://gateway.example?token=secret",
        "https://gateway.example/v1/mcp",
        "https://gateway.example/#secret",
    ],
)
def test_invalid_url_rejected(url):
    with pytest.raises(ValueError):
        normalize_url(url)


async def test_model_discovery_preserves_provider_metadata(server):
    async def handler(request):
        assert request.path == "/v1/models"
        return web.json_response(
            {
                "data": [
                    {
                        "id": "provider/model",
                        "owned_by": "provider",
                        "compatibility": {"openai_chat": True},
                    }
                ]
            }
        )

    async with ClientSession() as session:
        client = ApertureClient(session, await server(handler))
        models = await client.models()
    assert models[0]["id"] == "provider/model"
    assert models[0]["compatibility"]["openai_chat"] is True


async def test_sse_handles_comments_crlf_and_multiline_data(server):
    async def handler(request):
        assert (await request.json())["model"] == "provider/model"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for part in [
            b': keepalive\r\n\r\ndata: {"choices":\n',
            b'data: [{"delta":{"content":"Hello"},"finish_reason":"stop"}]}\r\n\r\ndata: [DONE]\n\n',
        ]:
            await response.write(part)
        return response

    async with ClientSession() as session:
        client = ApertureClient(session, await server(handler))
        output = [
            x
            async for x in client.chat(
                "chat_completions",
                "provider/model",
                [{"role": "user", "content": "hi"}],
                [],
                100,
            )
        ]
    assert "".join(x.get("content", "") for x in output) == "Hello"


async def test_auth_errors_are_sanitized_and_not_retried(server):
    requests = []

    async def handler(request):
        requests.append(request)
        return web.json_response(
            {"error": {"message": "secret-key prompt text"}}, status=403
        )

    async with ClientSession() as session:
        client = ApertureClient(session, await server(handler), "secret-key")
        with pytest.raises(ApertureError) as error:
            await client.models()
    assert error.value.status == 403
    assert "secret" not in str(error.value)
    assert len(requests) == 1


async def test_redirect_is_not_followed_with_credential(server):
    async def handler(request):
        return web.Response(status=302, headers={"Location": "https://other.example"})

    async with ClientSession() as session:
        client = ApertureClient(session, await server(handler), "secret-key")
        with pytest.raises(ApertureError):
            await client.models()
