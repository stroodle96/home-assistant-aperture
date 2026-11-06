"""UI configuration for a generic Aperture gateway and model subentries."""

from urllib.parse import urlsplit

import probatio
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigSubentryFlow,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_LLM_HASS_API,
    CONF_MODEL,
    CONF_PROMPT,
    CONF_URL,
)
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import ApertureClient, ApertureError, normalize_url
from .const import (
    CONF_ENABLE_MCP,
    CONF_MAX_TOKENS,
    CONF_PROTOCOL,
    CONF_TEMPERATURE,
    CONF_TIMEOUT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .protocols import PROTOCOLS


def _connection_schema(defaults):
    return probatio.Schema(
        {
            probatio.Required(
                CONF_URL, default=defaults.get(CONF_URL, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            probatio.Optional(probatio.Secret(CONF_API_KEY)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            probatio.Required(
                CONF_ENABLE_MCP, default=defaults.get(CONF_ENABLE_MCP, True)
            ): BooleanSelector(),
            probatio.Required(
                CONF_TIMEOUT, default=defaults.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=10, max=600, step=1, mode=NumberSelectorMode.BOX
                )
            ),
        }
    )


class ApertureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect a user-supplied gateway without changing its grants or networking."""

    VERSION = 1

    async def _validate(self, data):
        try:
            data[CONF_URL] = normalize_url(data[CONF_URL])
            if data.get(CONF_API_KEY):
                data[CONF_API_KEY] = data[CONF_API_KEY].strip()
            await ApertureClient(
                async_get_clientsession(self.hass),
                data[CONF_URL],
                data.get(CONF_API_KEY),
                timeout=10,
            ).models()
        except ValueError:
            return {"base": "invalid_url"}
        except ApertureError as err:
            return {
                "base": "invalid_auth"
                if err.status == 401
                else "access_denied"
                if err.status == 403
                else "cannot_connect"
            }
        return {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            errors = await self._validate(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_URL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Aperture ({urlsplit(user_input[CONF_URL]).hostname})",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            # Check the destination before a stored credential is sent anywhere.
            if not self._same_gateway(data, entry):
                return self.async_abort(reason="wrong_gateway")
            errors = await self._validate(data)
            if not errors:
                await self.async_set_unique_id(data[CONF_URL])
                self._abort_if_unique_id_mismatch(reason="wrong_gateway")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(entry.data),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            if not self._same_gateway(data, entry):
                return self.async_abort(reason="wrong_gateway")
            errors = await self._validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_connection_schema(entry.data),
            errors=errors,
        )

    @staticmethod
    def _same_gateway(data, entry):
        try:
            return normalize_url(data[CONF_URL]) == entry.data[CONF_URL]
        except ValueError:
            return False

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {
            "conversation": ApertureSubentryFlow,
            "ai_task_data": ApertureSubentryFlow,
        }


class ApertureSubentryFlow(ConfigSubentryFlow):
    """Create multiple independently configured conversation and AI task models."""

    async def async_step_user(self, user_input=None):
        return await self.async_step_options(user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self.async_step_options(user_input)

    async def async_step_options(self, user_input=None):
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        is_new = self.source == "user"
        if user_input is not None:
            user_input[CONF_MAX_TOKENS] = int(user_input[CONF_MAX_TOKENS])
            if is_new:
                return self.async_create_entry(
                    title=f"{user_input[CONF_MODEL]} ({self._subentry_type})",
                    data=user_input,
                )
            return self.async_update_and_abort(
                entry, self._get_reconfigure_subentry(), data=user_input
            )
        defaults = {} if is_new else dict(self._get_reconfigure_subentry().data)
        await entry.runtime_data.coordinator.async_request_refresh()
        models = entry.runtime_data.coordinator.data["models"]
        model_ids = sorted({m["id"] for m in models})
        default_model = defaults.get(CONF_MODEL, model_ids[0] if model_ids else "")
        schema = {
            probatio.Required(CONF_MODEL, default=default_model): SelectSelector(
                SelectSelectorConfig(options=model_ids, custom_value=True)
            ),
            probatio.Required(
                CONF_PROTOCOL, default=defaults.get(CONF_PROTOCOL, "chat_completions")
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(PROTOCOLS), mode=SelectSelectorMode.DROPDOWN
                )
            ),
            probatio.Required(
                CONF_MAX_TOKENS,
                default=defaults.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=128000, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            probatio.Optional(
                CONF_TEMPERATURE,
                description={"suggested_value": defaults.get(CONF_TEMPERATURE)},
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=2, step=0.1, mode=NumberSelectorMode.BOX
                )
            ),
        }
        if self._subentry_type == "conversation":
            apis = llm.async_get_apis(self.hass)
            available = {a.id for a in apis}
            selected = defaults.get(
                CONF_LLM_HASS_API,
                [f"{DOMAIN}_{entry.entry_id}"] if entry.runtime_data.mcp else [],
            )
            schema[
                probatio.Optional(
                    CONF_LLM_HASS_API, default=[a for a in selected if a in available]
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[{"label": a.name, "value": a.id} for a in apis],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
            schema[
                probatio.Optional(
                    CONF_PROMPT,
                    default=defaults.get(CONF_PROMPT, llm.DEFAULT_INSTRUCTIONS_PROMPT),
                )
            ] = TemplateSelector()
        return self.async_show_form(
            step_id="options", data_schema=probatio.Schema(schema)
        )
