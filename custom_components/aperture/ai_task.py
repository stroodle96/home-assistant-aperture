"""Text, structured JSON, and image-input AI tasks through Aperture."""

import json

import probatio
from homeassistant.components import ai_task, conversation
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .entity import ApertureEntity
from .protocols import restore_optional_fields

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    for subentry in entry.subentries.values():
        if subentry.subentry_type == "ai_task_data":
            async_add_entities(
                [ApertureTaskEntity(entry, subentry)],
                config_subentry_id=subentry.subentry_id,
            )


class ApertureTaskEntity(ApertureEntity, ai_task.AITaskEntity):
    """Provide the standard ai_task.generate_data action."""

    _attr_supported_features = (
        ai_task.AITaskEntityFeature.GENERATE_DATA
        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )

    async def _async_generate_data(self, task, chat_log):
        schema = (
            probatio.to_openapi(
                task.structure,
                custom_serializer=llm.selector_serializer,
                openapi_version="3.1.0",
            )
            if task.structure is not None
            else None
        )
        await self._async_handle_chat_log(chat_log, schema)
        last = chat_log.content[-1]
        if not isinstance(last, conversation.AssistantContent) or last.content is None:
            raise HomeAssistantError("Aperture did not return an AI task answer")
        data = last.content
        if schema is not None:
            try:
                data = json.loads(data)
                data = restore_optional_fields(data, schema)
                data = task.structure(data)
            except (ValueError, probatio.Invalid) as err:
                raise HomeAssistantError(
                    "Aperture returned invalid structured task data"
                ) from err
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=data
        )
