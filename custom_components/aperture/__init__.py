"""Aperture models and MCP tools for Home Assistant."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .client import ApertureClient
from .const import CONF_ENABLE_MCP, CONF_TIMEOUT, DEFAULT_TIMEOUT, DOMAIN

if TYPE_CHECKING:
    import httpx2
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import ApertureCoordinator
    from .mcp_client import ApertureMCPClient

PLATFORMS = ("conversation", "ai_task")
type ApertureConfigEntry = ConfigEntry[ApertureRuntime]


@dataclass
class ApertureRuntime:
    """Runtime resources owned by one integration instance."""

    client: ApertureClient
    mcp: ApertureMCPClient | None
    coordinator: ApertureCoordinator
    http_client: httpx2.AsyncClient | None


async def async_setup_entry(hass: HomeAssistant, entry: ApertureConfigEntry) -> bool:
    """Validate connections and register independent models and tools."""
    (
        ApertureCoordinator,
        ApertureToolsAPI,
        ApertureMCPClient,
    ) = await hass.async_add_executor_job(_prepare_runtime_modules)
    import httpx2
    from homeassistant.const import CONF_API_KEY, CONF_URL
    from homeassistant.helpers import llm
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.util.ssl import get_default_context

    client = ApertureClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data.get(CONF_API_KEY),
        entry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )
    http_client = None
    mcp = None
    if entry.data.get(CONF_ENABLE_MCP, True):
        # The SDK's response validation imports bundled JSON schemas lazily.
        await hass.async_add_executor_job(_prepare_jsonschema)
        http_client = httpx2.AsyncClient(
            headers=client.headers,
            verify=get_default_context(),
            follow_redirects=False,
            timeout=client.timeout.total,
        )
        mcp = ApertureMCPClient(f"{client.url}/mcp", http_client, client.timeout.total)
    coordinator = ApertureCoordinator(hass, entry, client, mcp)
    entry.runtime_data = ApertureRuntime(client, mcp, coordinator, http_client)
    try:
        await coordinator.async_config_entry_first_refresh()
        # Also poll when only another integration consumes our MCP API.
        entry.async_on_unload(coordinator.async_add_listener(lambda: None))
        if mcp:
            entry.async_on_unload(
                llm.async_register_api(
                    hass,
                    ApertureToolsAPI(
                        hass=hass,
                        id=f"{DOMAIN}_{entry.entry_id}",
                        name=f"{entry.title} MCP tools",
                        coordinator=coordinator,
                    ),
                )
            )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if http_client:
            await http_client.aclose()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _prepare_jsonschema() -> None:
    from jsonschema.validators import Draft202012Validator

    Draft202012Validator.check_schema({})


def _prepare_runtime_modules():
    """Load SDK and schema dependencies off the Home Assistant event loop."""
    from .coordinator import ApertureCoordinator
    from .llm import ApertureToolsAPI
    from .mcp_client import ApertureMCPClient

    return ApertureCoordinator, ApertureToolsAPI, ApertureMCPClient


async def _async_update_listener(
    hass: HomeAssistant, entry: ApertureConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ApertureConfigEntry) -> bool:
    """Unload entities before disposing of this entry's resources."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if entry.runtime_data.http_client:
        await entry.runtime_data.http_client.aclose()
    return True
