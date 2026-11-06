# Aperture for Home Assistant

An independent, community integration for [Aperture by Tailscale](https://tailscale.com/docs/aperture). Use your gateway's models in Assist and AI tasks, and let Home Assistant conversation agents discover and call Aperture MCP tools.

This project is not affiliated with or endorsed by Tailscale or Home Assistant. Requires **Home Assistant 2026.11.0 or later**. Version 0.1.0 is an initial release; upstream model features vary.

## Features

- Multiple conversation agents and AI task entities, each with its own model and API format.
- Streamed Assist replies and multi-step tool use through Home Assistant's ChatLog.
- An independent MCP connection: paginated discovery, native AI tools selection, original tool schemas and descriptions, and complete MCP results.
- MCP tools usable by other Home Assistant LLM agents that support the AI tools selector.
- Text and structured JSON AI tasks, plus image inputs for vision-capable models.
- UI setup, subentry reconfiguration, reauthentication, 30-minute catalog refresh, and credential-free diagnostics.

## Prerequisites

Home Assistant Core must be able to reach your Aperture gateway. On HA OS, installing the Tailscale app alone does not guarantee that Core can reach tailnet hosts: use a supported host-routing or proxy arrangement. Verify the route from Core, not just from a laptop or the app's container. The connection uses the device or proxy's Tailscale identity; Aperture grants must allow that identity to use the chosen models and MCP connectors.

Aperture forwards a model's native API. It does **not** convert arbitrary provider formats. Choose a model and matching format:

| Integration setting | Aperture endpoint | Required provider compatibility |
| --- | --- | --- |
| `chat_completions` | `/v1/chat/completions` | `openai_chat` |
| `responses` | `/v1/responses` | `openai_responses` |
| `anthropic` | `/v1/messages` | `anthropic_messages` |

Model discovery is not proof that a particular format, tool calling, vision, or JSON schema output is supported. Native Gemini and Bedrock APIs, audio transcription, speech generation, image generation, interactive MCP elicitation, and MCP resources/prompts are outside this release. Vision tasks accept images, not PDFs or audio. The MCP transport is Streamable HTTP.

## Install

For a manual install, copy `custom_components/aperture` into your Home Assistant `/config/custom_components` directory, then restart Home Assistant.

For HACS, add `https://github.com/stroodle96/home-assistant-aperture` as a custom **Integration** repository. This repository is structured for HACS; it is not in the default HACS catalog. A release's `aperture.zip` contains the files HACS installs into the integration directory.

Download the [latest manual-install ZIP](https://github.com/stroodle96/home-assistant-aperture/releases/latest/download/aperture-manual.zip) or [HACS ZIP](https://github.com/stroodle96/home-assistant-aperture/releases/latest/download/aperture.zip).

1. Open **Settings → Devices & services → Add integration → Aperture**.
2. Enter your gateway origin or `/v1` URL. Leave the optional credential empty for Tailscale identity authentication. If your gateway requires a bearer credential, enter it here; this is not an upstream provider setup screen. HTTP is supported for local bridges; use HTTPS for remote connections.
3. Keep **Connect to Aperture MCP tools** enabled to establish the integration's own tool connection.
4. On the integration, choose **Add service → Conversation agent**. Choose a model and its matching API format. Select **Aperture … MCP tools** under **AI tools**; also select **Device control** for native home control.
5. Choose this conversation entity in **Settings → Voice assistants** for your assistant pipeline. Continue using your existing speech-to-text and text-to-speech providers.
6. Use **Add service → AI task** to add one or more task models. Select an entity in the AI task defaults or target it explicitly in automations.

No existing MCP connection is required. If you already configured the built-in MCP Client against Aperture, switch your agents' AI tools selections to this integration before disabling that old entry. Keeping both selected duplicates tools.

Example action:

```yaml
action: ai_task.generate_data
data:
  entity_id: ai_task.your_aperture_model
  task_name: Brief summary
  instructions: Summarize the provided information in one sentence.
response_variable: summary
```

Use the AI task action editor to define a structured response or attach an image. The chosen upstream model must support those features. Maximum output tokens is configurable; leaving temperature unset supports models that reject that parameter. Generation and tool execution have no automatic retries, so a timeout cannot silently replay an action.

## Access and privacy

Aperture remains responsible for identity, connector permissions, model routing, provider credentials, auditing, and budgets. This integration does not change grants or network exposure. Selecting its MCP tools gives that conversation agent access to the tools granted to the identity connecting to Aperture. For a household voice assistant, grant the tools that assistant should have.

The integration does not log prompt bodies, credentials, tool arguments, or results. Home Assistant chat history, debug logging in Home Assistant or the MCP library, and Aperture/provider logging have their own retention settings. Diagnostics contain counts and protocol selections only.

## Development

```sh
python -m venv .venv
# Activate the environment, then:
python -m pip install -r requirements-dev.txt
python -m pytest
python -m ruff check custom_components tests scripts
python scripts/package.py
```

The protocol tests use local HTTP fixtures and never call paid models. `tests/ha_smoke.py` additionally runs real Home Assistant config flows, entity setup, Assist tool use, AI tasks, and unload/reload inside the official HA container. CI runs both. Deployment credentials and private verification artifacts must remain outside the public package.

See [Tailscale provider compatibility](https://tailscale.com/docs/aperture/reference/supported-providers-clients), [Aperture MCP proxying](https://tailscale.com/docs/aperture/mcp-server), and [Home Assistant's LLM API](https://developers.home-assistant.io/docs/core/llm/) for the upstream interfaces.
