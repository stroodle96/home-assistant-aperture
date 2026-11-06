"""Model and MCP catalog refresh."""

import logging
from datetime import timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import ApertureError
from .const import DOMAIN

LOGGER = logging.getLogger(__name__)


class ApertureCoordinator(DataUpdateCoordinator):
    """Refresh models and tools without making paid model requests."""

    def __init__(self, hass, entry, client, mcp):
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=30),
            always_update=True,
        )
        self.client = client
        self.mcp = mcp

    async def _async_update_data(self):
        try:
            models = await self.client.models()
            tools = await self.mcp.list_tools() if self.mcp else []
            return {"models": models, "tools": tools}
        except ApertureError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed(str(err)) from err
            raise UpdateFailed(str(err)) from err
