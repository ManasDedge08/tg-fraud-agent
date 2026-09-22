"""TigerGraph MCP client. The agent reaches the graph as MCP tools: it starts the
`tigergraph-mcp` server over stdio and calls `tigergraph__run_installed_query`,
`tigergraph__add_node` and `tigergraph__add_edges` like any other tool.

One server process per run, driven from a background event loop so the rest of the
agent stays synchronous.
"""
import asyncio
import json
import os
import logging
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.join(os.path.dirname(__file__), "..")
logging.getLogger("pyTigerGraph").setLevel(logging.ERROR)
BIN = os.path.join(ROOT, ".venv", "bin", "tigergraph-mcp")


class MCPGraph:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self._ready = asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=120)
        self.graph = os.environ.get("TG_GRAPHNAME", "Fraud")

    async def _start(self):
        params = StdioServerParameters(command=BIN, args=["--env-file", os.path.join(ROOT, ".env")],
                                       env={**os.environ, "TG_LOG_TOOL_CALLS": "false"})
        self._stdio = stdio_client(params)
        read, write = await self._stdio.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        tools = await self._session.list_tools()
        self.tools = [t.name for t in tools.tools]
        return True

    def call(self, name, **args):
        async def go():
            r = await self._session.call_tool(name, args)
            text = "".join(getattr(c, "text", "") for c in r.content).strip()
            if text.startswith("```"):  # server wraps a JSON envelope in a fenced block, then a markdown recap
                text = text.split("\n", 1)[1].split("```", 1)[0]
            try:
                return json.loads(text)
            except ValueError:
                return text
        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(timeout=int(os.environ.get("MCP_TIMEOUT", "60")))

    # --- the calls the agent makes -------------------------------------------
    def query(self, name, **params):
        return self.call("tigergraph__run_installed_query", graph_name=self.graph, query_name=name, params=params)

    def add_node(self, vtype, vid, attrs):
        return self.call("tigergraph__add_node", graph_name=self.graph, vertex_type=vtype, vertex_id=vid, attributes=attrs)

    def add_edges(self, etype, src_type, tgt_type, pairs):
        edges = [{"source_type": src_type, "source_id": s, "target_type": tgt_type, "target_id": t} for s, t in pairs]
        return self.call("tigergraph__add_edges", graph_name=self.graph, edge_type=etype, edges=edges)


_client = None


def client():
    global _client
    if _client is None:
        _client = MCPGraph()
    return _client
