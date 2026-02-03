# qa_agent/tools/mcp_loader.py
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_mcp_adapters.tools import load_mcp_tools

async def load_tools_via_mcp(command: str, args: list[str]):
    server_params = StdioServerParameters(command=command, args=args)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=10.0)
            lc_tools = await asyncio.wait_for(load_mcp_tools(session), timeout=30.0)
            tool_name_map = {t.name: t for t in lc_tools}
            return session, lc_tools, tool_name_map
