"""MCP server for the paper desk: read tools (data is DELAYED) and, kept apart, trade tools behind the desk's gates.

Run it with `python -m desk.mcp_server` (stdio). It talks to the IB paper Gateway as client id 20 and refuses any
non-paper port, host or account (the lab's `connect_paper` does the checking).
"""
