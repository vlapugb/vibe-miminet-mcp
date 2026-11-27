"""Create and verify a two-host network through MCP after interactive sign-in."""

import asyncio
import os
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def create_example(call):
    network = await call("create_network", title="MCP demo")
    guid = network["guid"]
    print(f"Created network: {network['url']}", flush=True)

    switch = await call("add_device", guid=guid, device="switch", x=50, y=35)
    hosts = {}
    for x, address in [(25, "10.0.0.1"), (75, "10.0.0.2")]:
        host = await call("add_device", guid=guid, device="host", x=x, y=65)
        device_id = host["device_id"]
        await call(
            "connect_devices", guid=guid, source=device_id, target=switch["device_id"]
        )
        hosts[device_id] = address

    graph = await call("get_network", guid=guid)
    for node in graph["devices"]:
        if node["id"] in hosts:
            await call(
                "configure_interface",
                guid=guid,
                device_id=node["id"],
                interface_id=node["interfaces"][0]["id"],
                address=hosts[node["id"]] + "/24",
            )

    graph = await call("get_network", guid=guid)
    stored = {
        n["id"]: (n["interfaces"][0]["ip"], n["interfaces"][0]["netmask"])
        for n in graph["devices"]
        if n["id"] in hosts and n["interfaces"]
    }
    links = {frozenset((e["source"], e["target"])) for e in graph["links"]}
    if (
        len(graph["devices"]) != 3
        or len(graph["links"]) != 2
        or stored != {key: (ip, 24) for key, ip in hosts.items()}
        or links != {frozenset((key, switch["device_id"])) for key in hosts}
    ):
        raise RuntimeError(
            "The saved topology or addresses differ from the requested network."
        )
    print(
        f"Verified two hosts, their addresses and both links: {network['url']}",
        flush=True,
    )
    return graph


async def main():
    desktop = (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "MIMINET_URL",
        "MIMINET_PROFILE",
        "PLAYWRIGHT_BROWSERS_PATH",
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "miminet_mcp.server"],
        env={key: os.environ[key] for key in desktop if key in os.environ},
    )
    async with Client(params, read_timeout_seconds=90) as client:

        async def call(name, **arguments):
            result = await client.call_tool(name, arguments)
            if result.is_error or result.structured_content is None:
                message = " ".join(c.text for c in result.content if c.type == "text")
                raise RuntimeError(f"{name}: {message}")
            return result.structured_content

        while (await call("open_miminet"))["status"] != "signed_in":
            await asyncio.to_thread(
                input, "Sign in to Miminet in Firefox, then press Enter here. "
            )
        await create_example(call)
        await asyncio.to_thread(
            input, "Press Enter to close Firefox. The network stays in My Networks. "
        )


if __name__ == "__main__":
    asyncio.run(main())
