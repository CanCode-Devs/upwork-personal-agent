import asyncio

from app.upwork.mcp_catalog import sync_catalog_after_connect
from app.upwork.mcp_client import UpworkMcpClient


async def main() -> None:
    client = UpworkMcpClient()
    tools = await client.login()
    catalog = sync_catalog_after_connect(tools)
    print("Logged in to Upwork MCP.")
    if tools:
        print("Tools:")
        for tool in tools:
            print(f"  - {tool.name}")
    else:
        print("No tools returned.")
    pending = catalog.pending
    if pending is not None and pending.has_changes():
        print(
            f"MCP catalog changed: +{len(pending.added)} -{len(pending.removed)} ~{len(pending.changed)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
