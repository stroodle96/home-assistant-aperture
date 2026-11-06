"""Diagnostics deliberately exclude credentials, URLs, prompts and tool results."""

from .const import CONF_ENABLE_MCP, CONF_PROTOCOL, CONF_TIMEOUT


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    data = runtime.coordinator.data or {}
    return {
        "version": "0.1.0",
        "mcp_enabled": entry.data.get(CONF_ENABLE_MCP, True),
        "timeout": entry.data.get(CONF_TIMEOUT),
        "last_update_success": runtime.coordinator.last_update_success,
        "model_count": len(data.get("models", [])),
        "tool_count": len(data.get("tools", [])),
        "entities": [
            {"type": sub.subentry_type, "protocol": sub.data.get(CONF_PROTOCOL)}
            for sub in entry.subentries.values()
        ],
    }
