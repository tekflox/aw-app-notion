"""Entry describing this app's own ``/mcp`` endpoint, for aw-mcp-gateway's
app-scan (``scan_app_mcp_servers()``, which reads
``<installed-app-dir>/mcp.json``).

Mirrors ``aw-app-diff-tool``'s ``diff_app/mcp/self_register.py`` — including
*why* it is an HTTP endpoint rather than the monolith's stdio script. The
monolith's ``src/mcp/kanban.py`` was a subprocess the gateway spawned, which
then called back into awserv over HTTP using an API key it read off disk from
``.tmp/awserv_api_key``. Reproducing that here would mean the gateway
container holding a workspace credential it has no path to obtain. Serving MCP
from this app's own already-authenticated route sidesteps the whole problem.

Unlike diff-tool this module only *builds* the entry; ``mcp_config.py`` owns
writing ``mcp.json``, because this app has a second server (the upstream
Notion one) in the same file and two independent writers would race.
"""
from __future__ import annotations

import os
import socket

MCP_SERVER_NAME = "aw-kanban"
ROUTE_PATH = "/api/apps/notion/mcp"


def build_self_entry(port: int | None = None) -> dict:
    """The ``mcpServers`` entry pointing at this app's ``POST /mcp``.

    Tier-1 (in-process): this *is* the aw-workspace process, so
    ``socket.gethostname()`` is exactly the value ContainerSupervisor injects
    into sibling containers as ``AW_WORKSPACE_HOST``, and
    ``AW_WORKSPACE_API_KEY`` is already in this process's own environment —
    nothing has to be provisioned.
    """
    host = socket.gethostname()
    port = port or int(os.environ.get("AW_PORT") or 9030)
    entry: dict = {
        "type": "http",
        "url": f"http://{host}:{port}{ROUTE_PATH}",
        "enabled": True,
    }
    api_key = os.environ.get("AW_WORKSPACE_API_KEY")
    if api_key:
        entry["headers"] = {"X-Api-Key": api_key}
    return entry
