"""This app's own MCP surface — the Kanban tools, over Streamable HTTP.

Distinct from the ``notion`` server in ``notion_app/mcp_config.py``: that one
is the official upstream ``@notionhq/notion-mcp-server``, spawned by MCP
Gateway as an ``npx`` subprocess. This one is served by this app's own FastAPI
routes, in-process, and both are advertised from the same generated
``mcp.json``.
"""
