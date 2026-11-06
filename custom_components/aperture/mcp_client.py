"""An independent Aperture MCP client backed by the official SDK."""

import asyncio
import re
from contextlib import asynccontextmanager
from hashlib import sha256

import httpx2
from mcp import MCPError, types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .client import ApertureError


def safe_tool_name(entry_id: str, remote_name: str) -> str:
    """Avoid collisions and the 64 character provider tool name limit."""
    scope = sha256(entry_id.encode()).hexdigest()[:8]
    digest = sha256(remote_name.encode()).hexdigest()[:8]
    name = re.sub(r"[^a-zA-Z0-9_]", "_", remote_name)[:30]
    return f"aperture_{scope}_{name}_{digest}"


async def _raise_post_error(response: httpx2.Response) -> None:
    """Preserve HTTP status before the MCP SDK converts it to an RPC error."""
    if response.request.method == "POST":
        response.raise_for_status()


class ApertureMCPClient:
    """Open a fresh session per operation; never replay a failed call."""

    def __init__(
        self, url: str, http_client: httpx2.AsyncClient, timeout: float = 60
    ) -> None:
        self.url = url
        self.http_client = http_client
        if _raise_post_error not in http_client.event_hooks["response"]:
            http_client.event_hooks["response"].append(_raise_post_error)
        self.timeout = timeout

    @asynccontextmanager
    async def _session(self):
        try:
            async with (
                asyncio.timeout(self.timeout),
                streamable_http_client(self.url, http_client=self.http_client) as (
                    read,
                    write,
                ),
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=self.timeout,
                    client_info=types.Implementation(
                        name="home-assistant-aperture", version="0.1.0"
                    ),
                ) as session,
            ):
                await session.initialize()
                yield session
        except (ExceptionGroup, MCPError, httpx2.HTTPError, TimeoutError) as err:
            leaf = err
            while isinstance(leaf, ExceptionGroup):
                leaf = leaf.exceptions[0]
            status = (
                leaf.response.status_code
                if isinstance(leaf, httpx2.HTTPStatusError)
                else None
            )
            raise ApertureError(
                "Aperture MCP request failed; check connectivity, credentials and connector grants",
                status,
            ) from None

    async def list_tools(self) -> list[types.Tool]:
        """Read all discovery pages with bounded and cyclic-cursor protection."""
        async with self._session() as session:
            tools = []
            cursor = None
            cursors = set()
            for _ in range(100):
                page = await session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor)
                    if cursor
                    else None
                )
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    return tools
                if cursor in cursors:
                    raise ApertureError(
                        "Aperture MCP returned a repeated pagination cursor"
                    )
                cursors.add(cursor)
            raise ApertureError("Aperture MCP tool discovery exceeded 100 pages")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Return complete MCP content and structured results, including isError."""
        async with self._session() as session:
            result = await session.call_tool(
                name, arguments, read_timeout_seconds=self.timeout
            )
            if not isinstance(result, types.CallToolResult):
                raise ApertureError(
                    "MCP tool requires an unsupported interactive response"
                )
            return result.model_dump(by_alias=True, exclude_none=True)
