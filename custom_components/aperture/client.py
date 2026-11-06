"""Asynchronous Aperture model transport, with bounded and sanitized errors."""

import json
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .protocols import build_request, transform_stream

MAX_EVENT_BYTES = 8 * 1024 * 1024


class ApertureError(Exception):
    """Gateway error without request bodies, credentials or provider messages."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def normalize_url(url: str) -> str:
    """Accept a gateway origin or API root and return exactly one /v1 suffix."""
    parts = urlsplit(url.strip())
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Enter an HTTP(S) gateway URL without credentials, query or fragment"
        )
    _ = parts.port
    path = parts.path.rstrip("/")
    if path.endswith(("/mcp", "/chat/completions", "/responses", "/messages")):
        raise ValueError("Enter the gateway URL, rather than a model or MCP endpoint")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


async def sse_events(response: aiohttp.ClientResponse) -> AsyncIterator[dict]:
    """Parse UTF-8 SSE frames, including multiline events and comments."""
    buffer = b""
    data: list[str] = []
    size = 0
    async for chunk in response.content.iter_any():
        buffer += chunk
        if len(buffer) + size > MAX_EVENT_BYTES:
            raise ApertureError("Gateway stream event exceeded the size limit")
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data:
                    payload = "\n".join(data)
                    data, size = [], 0
                    if payload == "[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except (ValueError, UnicodeError) as err:
                        raise ApertureError(
                            "Gateway returned invalid streaming JSON"
                        ) from err
                    if not isinstance(event, dict):
                        raise ApertureError("Gateway returned an invalid stream event")
                    yield event
            elif line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                data.append(value.decode("utf-8"))
                size += len(value)
    if buffer or data:
        raise ApertureError("Gateway stream ended inside an event")


class ApertureClient:
    """Use Home Assistant's shared aiohttp session without owning its lifecycle."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str | None = None,
        timeout: float = 120,
    ) -> None:
        self.session = session
        self.url = normalize_url(url)
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout, sock_read=timeout)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def check_response(self, response: aiohttp.ClientResponse) -> None:
        if not 200 <= response.status < 300:
            hints = {
                401: "Authentication failed",
                403: "Access denied; check the device's Aperture grants",
                404: "Endpoint or selected model API format is unavailable",
                429: "Gateway quota or rate limit reached",
            }
            raise ApertureError(
                hints.get(response.status, "Gateway request failed")
                + f" (HTTP {response.status})",
                response.status,
            )

    async def models(self) -> list[dict]:
        """Fetch granted model IDs and retain provider metadata."""
        try:
            async with self.session.get(
                f"{self.url}/models",
                headers=self.headers,
                timeout=self.timeout,
                allow_redirects=False,
            ) as response:
                self.check_response(response)
                data = await response.json()
            models = data["data"]
            if not isinstance(models, list) or any(
                not isinstance(m, dict) or not isinstance(m.get("id"), str)
                for m in models
            ):
                raise ValueError
            return models
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ApertureError(
                "Unable to reach Aperture; check connectivity and TLS"
            ) from err
        except (ValueError, KeyError, TypeError) as err:
            raise ApertureError("Gateway returned an invalid model list") from err

    async def chat(
        self,
        protocol: str,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        *,
        schema: dict | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[dict]:
        """Generate once, with no implicit replay after a failure."""
        path, body = build_request(
            protocol,
            model,
            messages,
            tools,
            max_tokens,
            schema=schema,
            temperature=temperature,
        )
        headers = {**self.headers, "Accept": "text/event-stream"}
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        try:
            async with self.session.post(
                f"{self.url}/{path}",
                headers=headers,
                json=body,
                timeout=self.timeout,
                allow_redirects=False,
            ) as response:
                self.check_response(response)
                if response.content_type != "text/event-stream":
                    raise ApertureError(
                        "Gateway did not return a streaming response; verify the model API format"
                    )
                async for delta in transform_stream(protocol, sse_events(response)):
                    yield delta
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ApertureError("Aperture generation failed or timed out") from err
        except (ValueError, KeyError, TypeError, UnicodeError) as err:
            raise ApertureError(
                "Aperture returned invalid or incomplete model output"
            ) from err
