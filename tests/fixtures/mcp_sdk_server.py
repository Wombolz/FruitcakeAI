"""Controlled SDK peer for Fruitcake's transport integration tests."""
import asyncio
import os
import sys
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver import Context


def make_server():
    server = MCPServer("fruitcake-transport-test")

    @server.tool()
    async def echo(value: str) -> str:
        return value

    @server.tool()
    async def identity() -> dict:
        return {"pid": os.getpid(), "cwd": os.getcwd(), "env": os.getenv("FIXTURE_VALUE")}

    @server.tool()
    async def fail() -> str:
        raise ValueError("intentional tool failure")

    @server.tool()
    async def slow_mutation(marker: str) -> str:
        with Path(marker).open("a") as out:
            out.write("executed\n")
        await asyncio.sleep(30)
        return "done"

    @server.tool()
    async def change_tools(ctx: Context) -> str:
        def added() -> str:
            return "new tool"
        server.add_tool(added)
        server.remove_tool("echo")
        await ctx.notify_tools_changed()
        return "changed"

    return server


if __name__ == "__main__":
    print("fixture started", file=sys.stderr, flush=True)
    make_server().run()
