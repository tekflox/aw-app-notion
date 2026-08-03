# aw-app-notion

AW workspace app that ports agentic-workspace's generic Notion MCP
integration (`src/config/mcp.json`'s `notion` entry, the official
`@notionhq/notion-mcp-server`) into the decoupled-apps model. Stores a
Notion internal-integration token in the zero-knowledge secret store and
regenerates this app's own `mcp.json` on disk from it — the file
`aw-mcp-gateway`'s app-scan reads directly (same contract as
`aw-app-mcp-tools`).

This is **not** `aw-kanban` (agentic-workspace's bespoke wrapper around one
specific Notion board via awserv) — this is the generic, official Notion
MCP, usable against any page/database shared with the integration. See
`skills/aw-notion/SKILL.md` for the tool-usage reference and how the two
compare.

## Layout

- `aw-app.json` — manifest (id `notion`, tier `inprocess`, depends on
  `mcp-gateway`).
- `notion_app/plugin.py` — `NotionAppPlugin`; `activate(ctx)` regenerates
  `mcp.json` from whatever token is already in `ctx.secrets` (picks up a
  token saved before a workspace recreation) and mounts the settings
  sub-app via `ctx.routes`.
- `notion_app/mcp_config.py` — `build_mcp_servers(token)` /
  `write_mcp_json(package_dir, token)`: the `mcpServers.notion` shape
  (`npx -y @notionhq/notion-mcp-server`, `env.NOTION_TOKEN`), empty when no
  token is saved.
- `notion_app/routes.py` — `GET /status`, `POST /settings`,
  `POST /logout`, `GET /mcp.json`. No standalone mode (every route needs
  `ctx.secrets`/`ctx.package_dir`, same reasoning as `aw-app-git` having no
  `__main__.py`).
- `windows/main.json` — declarative settings window: setup instructions
  (create an internal integration, share pages with it) + a password-input
  form posting to `/settings`.
- `skills/aw-notion/SKILL.md` — agent-facing tool reference, shipped via
  `contributes.skills` (symlinked into the shared skills index on install).
- `tests/validate_manifest.py` — schema validation.
- `tests/test_routes.py` — `FastAPI TestClient` against `build_routes(ctx)`
  with a fake `ctx` (secrets facade only, matching `aw-app-git`'s test
  pattern) + unit tests for `mcp_config.build_mcp_servers`.

## Why the token isn't wired through the generic config endpoint

`config_schema.notion_token` is marked `x-secret: true` for the settings UI
(password input), but the actual save goes through this app's own
`POST /api/apps/notion/settings` — not the generic
`POST /api/apps/notion/config`. The generic endpoint stores whatever it's
given in `loaded.config`, which is plain (not secret-store) config that can
get synced to the cloud registry (`reconciler.cloud.put_desired`) if cloud
sync is configured. Same reasoning `aw-app-git` documents for
`github_token`: a real credential shouldn't take that path just because
`contributes.mcp.reload_on_save` would make hot-reload automatic if it did.

**Trade-off accepted:** because the token never touches the generic config
endpoint, `contributes.mcp.reload_on_save` (which only fires from that
endpoint — see `src/apps/routes.py`'s `save_app_config`) never fires for a
token save either. MCP Gateway picks up a freshly-saved token on its own
next `/reload` (triggered by some other app's config save) or on a
restart/reinstall of the **MCP Gateway** app — not instantly. This is a
deliberate safety-over-convenience call for this port; revisit if a
sanctioned app-to-app "ask core to reload the gateway" mechanism ever lands
(there isn't one today — `_reload_mcp_gateway` in `src/apps/routes.py` is
core-only, and reaching for it from app code would mean either a new
capability or spoofing core's own trusted internal header, neither of which
this port does).

## Testing done

1. **Manifest validation**: `python3 tests/validate_manifest.py` →
   `OK: aw-app.json is valid and all system_clis installers exist`.
2. **Route tests**: `python3 -m pytest tests/` — status/settings/logout/
   mcp.json round-trip through a fake `ctx`, plus `mcp_config` unit tests.
   No real Notion API calls (there's nothing to call — this app itself
   never talks to Notion; the MCP Gateway's spawned `npx` subprocess does).

## NOT done here (explicitly out of scope)

- No install into a running workspace — install and paste a real token
  manually after reviewing this.
- No automatic MCP Gateway hot-reload on token save — see above.
- No re-verification of the full `@notionhq/notion-mcp-server` tool list
  against a live `tools/list` call — the skill's tool table is sourced from
  the upstream project's own docs at port time and flagged as
  non-exhaustive.
