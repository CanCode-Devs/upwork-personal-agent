import asyncio

from app.upwork.mcp_client import UpworkMcpClient


async def main() -> None:
    client = UpworkMcpClient()
    tools = await client.login()
    print("Logged in to Upwork MCP.")
    if tools:
        print("Tools:")
        for name in tools:
            print(f"  - {name}")
    else:
        print("No tools returned.")


if __name__ == "__main__":
    asyncio.run(main())
