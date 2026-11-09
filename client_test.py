"""Simple local test client for the MCP server.

Usage (server must be running):
    python client_test.py
    python client_test.py http://127.0.0.1:3000/mcp
"""
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3000/mcp"


async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Tools:", [t.name for t in tools.tools])

            result = await session.call_tool("server_time", {})
            print("server_time:", result.content[0].text)

            result = await session.call_tool("mail_check", {})
            print("mail_check:", result.content[0].text)


asyncio.run(main())
