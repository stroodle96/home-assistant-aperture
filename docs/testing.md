# Verification

The initial version was verified with 35 automated protocol/HTTP tests and the official Home Assistant 2026.11.0 container, pinned to:

`ghcr.io/home-assistant/home-assistant@sha256:c6c1ca9931036bf3a2c4cc088d8db25dfea3b33935a5ecc83c40349462e2f69e`

The container smoke test exercises the real config flow and subentry flows, creates both entity types, discovers MCP tools, performs an Assist tool call and model follow-up, generates structured AI task data, and unloads/reloads the integration. It also checks that a rejected gateway URL never receives a saved credential, and that an unsupported connector schema does not prevent other tools from working.

All model and MCP responses in these automated tests come from a loopback fixture gateway. They do not prove every upstream provider/model combination, actual voice hardware behavior, production tailnet connectivity, or a successful deployment on an existing Home Assistant installation. Those depend on the selected models, grants, networking and Assist pipeline.

The HTTP tests cover paginated MCP discovery, preserved authentication failures, sanitized exceptions, no tool-call replay, fragmented SSE, delayed tool execution until successful stream completion, JSON schema adaptation, optional fields, and protocol-specific tool/result messages.

The CI workflow runs the unit suite, lint, formatting, the real Home Assistant smoke test, and ZIP packaging. Tagged releases require that validation job to pass.
