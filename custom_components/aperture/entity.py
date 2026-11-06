"""Shared Home Assistant ChatLog driver for Aperture entities."""

import base64
import json

import probatio
from homeassistant.components import conversation
from homeassistant.const import CONF_MODEL
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import llm
from homeassistant.helpers.entity import Entity

from .client import ApertureError
from .const import (
    CONF_MAX_TOKENS,
    CONF_PROTOCOL,
    CONF_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DOMAIN,
    MAX_TOOL_ITERATIONS,
)


def _encode_attachment(attachment):
    if not attachment.mime_type.startswith("image/"):
        raise HomeAssistantError(
            "Aperture supports image attachments; this attachment is not an image"
        )
    if attachment.path.stat().st_size > 20 * 1024 * 1024:
        raise HomeAssistantError("Image attachment exceeds 20 MiB")
    data = base64.b64encode(attachment.path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{attachment.mime_type};base64,{data}"},
    }


class ApertureEntity(Entity):
    """Common entity attributes and tool-enabled generation."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, entry, subentry):
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Tailscale",
            model=subentry.data[CONF_MODEL],
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            self.entry.runtime_data.coordinator.async_add_listener(
                self.async_write_ha_state
            )
        )

    @property
    def available(self):
        return self.entry.runtime_data.coordinator.last_update_success

    async def _messages(self, chat_log):
        messages = []
        for content in chat_log.content:
            if isinstance(content, conversation.ToolResultContent):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": content.tool_call_id,
                        "content": json.dumps(
                            {
                                "data": content.result.data,
                                "error": content.result.error,
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                continue
            message = {"role": content.role, "content": content.content}
            if isinstance(content, conversation.AssistantContent):
                if content.tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": tool.id,
                            "type": "function",
                            "function": {
                                "name": tool.tool_name,
                                "arguments": json.dumps(tool.tool_args),
                            },
                        }
                        for tool in content.tool_calls
                    ]
                native = content.native
                if (
                    isinstance(native, dict)
                    and native.get("aperture_model") == self.subentry.data[CONF_MODEL]
                ):
                    message["reasoning_items"] = native.get("reasoning_items", [])
            if isinstance(content, conversation.UserContent) and content.attachments:
                images = [
                    await self.hass.async_add_executor_job(_encode_attachment, a)
                    for a in content.attachments
                ]
                message["content"] = [
                    {"type": "text", "text": content.content or ""},
                    *images,
                ]
            messages.append(message)
        return messages

    async def _ha_stream(self, source):
        reasoning = []
        async for delta in source:
            if "reasoning_item" in delta:
                reasoning.append(delta.pop("reasoning_item"))
            if "tool_calls" in delta:
                delta["tool_calls"] = [
                    llm.ToolInput(
                        id=t["id"], tool_name=t["name"], tool_args=t["arguments"]
                    )
                    for t in delta["tool_calls"]
                ]
            if delta:
                yield delta
        if reasoning:
            yield {
                "native": {
                    "aperture_model": self.subentry.data[CONF_MODEL],
                    "reasoning_items": reasoning,
                }
            }

    async def _async_handle_chat_log(self, chat_log, schema=None):
        options = self.subentry.data
        tools = []
        if chat_log.llm_api:
            tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": probatio.to_openapi(
                        t.parameters,
                        custom_serializer=chat_log.llm_api.custom_serializer,
                        openapi_version="3.1.0",
                    ),
                }
                for t in chat_log.llm_api.tools
            ]
        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                source = self.entry.runtime_data.client.chat(
                    options[CONF_PROTOCOL],
                    options[CONF_MODEL],
                    await self._messages(chat_log),
                    tools,
                    int(options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)),
                    schema=schema,
                    temperature=options.get(CONF_TEMPERATURE),
                )
                async for _content in chat_log.async_add_delta_content_stream(
                    self.entity_id, self._ha_stream(source)
                ):
                    pass
            except ApertureError as err:
                if err.status == 401:
                    self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError(str(err)) from err
            if not chat_log.unresponded_tool_results:
                return
        raise HomeAssistantError("Aperture reached the maximum number of tool rounds")
