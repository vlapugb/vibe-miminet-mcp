"""Run the Miminet MCP server over stdio."""

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

from mcp.server import MCPServer

from .editor import Editor


def make_server(editor: Editor) -> MCPServer:
    server = MCPServer(
        "miminet",
        instructions=(
            "Start with open_miminet. If login is required, let the user finish in the browser. "
            "Use list_device_actions for device commands, including GRE and IPIP. Use configure_vxlan for VTEPs. "
            "After an uncertain write, read the network before retrying."
        ),
    )
    for tool in (
        editor.open_miminet,
        editor.create_network,
        editor.get_network,
        editor.configure_network,
        editor.add_device,
        editor.connect_devices,
        editor.configure_interface,
        editor.configure_device,
        editor.configure_link,
        editor.configure_switch,
        editor.configure_vxlan,
        editor.list_device_actions,
        editor.set_device_action,
        editor.remove_device_action,
        editor.remove_element,
    ):
        server.add_tool(tool)
    return server


async def run_server():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    profile = Path(
        os.getenv("MIMINET_PROFILE", "~/.local/share/miminet-mcp/firefox")
    ).expanduser()
    async with AsyncExitStack() as resources:
        editor = Editor(
            resources, os.getenv("MIMINET_URL", "https://miminet.ru"), profile
        )
        try:
            await make_server(editor).run_stdio_async()
        finally:
            if editor.context is not None:
                await editor.context.close()


def main():
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
