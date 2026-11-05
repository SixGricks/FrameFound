"""FrameFound as a set of tools Claude can call (the MCP connector).

Mounted on the API at `/api/mcp` (framefound/main.py), so it rides on the
same Caddy route, the same public-access gate and the same tailnet as the
web UI. Authentication is a panel token (auth/panel_tokens.py) with the
`mcp` host label; the `tag` scope is what lets Claude write tags, and
even then only as suggestions for the operator to judge.

See docs/mcp.md and ADR-0020.
"""
