"""Check action validation and discover the server tools over stdio."""

import asyncio
import sysconfig
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

from miminet_mcp.actions import action_parameters


def test_device_action_arguments():
    tunnel = {
        "local_ip": "192.0.2.1",
        "remote_ip": "192.0.2.2",
        "tunnel_ip": "10.0.0.1",
        "name": "tun0",
    }
    assert action_parameters("ipip", tunnel) == tunnel
    for action, values in [
        ("unknown", {}),
        ("gre", {**tunnel, "name": "bad name"}),
        ("gre", {**tunnel, "local_ip": "bad"}),
        ("gre", {**tunnel, "job_id": 1}),
        (
            "vlan_subinterface",
            {"ip": "10.0.0.1", "prefix": 24, "interface_id": "iface_1", "vlan": 4095},
        ),
    ]:
        with pytest.raises(ToolError):
            action_parameters(action, values)


def test_stdio(tmp_path):
    async def check():
        params = StdioServerParameters(
            command=str(Path(sysconfig.get_path("scripts")) / "miminet-mcp"),
            cwd=str(tmp_path),
        )
        async with Client(params, read_timeout_seconds=10) as agent:
            tools = {tool.name for tool in (await agent.list_tools()).tools}
            assert tools == {
                "open_miminet",
                "create_network",
                "configure_network",
                "get_network",
                "add_device",
                "connect_devices",
                "configure_interface",
                "configure_device",
                "configure_link",
                "configure_switch",
                "configure_vxlan",
                "list_device_actions",
                "set_device_action",
                "remove_device_action",
                "remove_element",
            }
            invalid = await agent.call_tool("get_network", {"guid": "invalid"})
            assert invalid.is_error

    asyncio.run(check())
