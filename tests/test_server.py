"""Exercise the MCP tools against the real editor and an isolated Flask database."""

import asyncio
import json
import os
import sysconfig
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

from examples.demo import create_example
from miminet_mcp.actions import ACTIONS, action_parameters
from miminet_mcp.server import Editor, make_server


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


def test_saved_network_in_browser(tmp_path, monkeypatch):
    upstream = os.environ.get("MIMINET_SOURCE")
    if not upstream:
        pytest.skip("Set MIMINET_SOURCE to a Miminet checkout to run the Firefox test.")
    source = Path(upstream).expanduser().resolve() / "front" / "src"
    assert (source / "app.py").is_file(), f"Miminet frontend missing at {source}"
    from sqlalchemy import Integer
    from werkzeug.security import generate_password_hash

    database = tmp_path / "test.sqlite"
    monkeypatch.syspath_prepend(str(source))
    monkeypatch.setenv("MODE", "dev")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", f"sqlite:///{database}")
    monkeypatch.setenv("EXTERNAL_BASE_URL", "/")
    monkeypatch.setenv("JWT_COOKIE_DOMAIN", "")
    monkeypatch.setenv("MOZ_HEADLESS", "1")
    monkeypatch.setenv(
        "PLAYWRIGHT_BROWSERS_PATH", os.getenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    )
    from app import app
    from database.models import Network, Simulate, User, db

    monkeypatch.setitem(app.config, "JWT_COOKIE_CSRF_PROTECT", True)
    monkeypatch.setattr(
        "miminet_network.PREVIEW_IMAGES_ROOT", str(tmp_path / "previews")
    )
    other_guid = str(uuid4())
    with app.app_context():
        assert db.engine.url.database == str(database)
        tables = [User.__table__, Network.__table__, Simulate.__table__]
        for table in tables:
            column = table.c.id
            monkeypatch.setattr(
                column, "type", column.type.with_variant(Integer, "sqlite")
            )
        db.metadata.create_all(db.engine, tables=tables)
        db.session.add_all(
            [
                User(
                    id=1,
                    nick="Student",
                    email="student@example.test",
                    password_hash=generate_password_hash("test"),
                ),
                User(id=2, nick="Other"),
            ]
        )
        db.session.flush()
        db.session.add(Network(author_id=2, guid=other_guid, share_mode=True))
        db.session.commit()

    origin = "https://miminet.test"
    http = app.test_client(use_cookies=False)

    async def serve(route):
        req = route.request
        url = urlsplit(req.url)
        if url.hostname != "miminet.test":
            return await route.abort()
        headers = await req.all_headers()
        response = http.open(
            url.path,
            query_string=url.query,
            method=req.method,
            headers=headers,
            data=req.post_data_buffer,
            base_url=origin,
        )
        outgoing = dict(response.headers)
        cookies = response.headers.getlist("Set-Cookie")
        if cookies:
            outgoing["Set-Cookie"] = "\n".join(cookies)
        if response.location:
            # Playwright bypasses route handlers for the next hop of an HTTP redirect.
            # Render Flask's actual destination at its final URL without opening a port.
            target = response.location
            response = http.get(target, headers=headers, base_url=origin)
            script = f"<head><script>history.replaceState(null, '', {json.dumps(target)})</script>"
            body = response.get_data().replace(b"<head>", script.encode(), 1)
            outgoing.pop("Location", None)
        else:
            response.direct_passthrough = False
            body = response.get_data()
        outgoing.pop("Content-Length", None)
        await route.fulfill(status=response.status_code, headers=outgoing, body=body)

    async def check():
        async with AsyncExitStack() as stack:
            editor = Editor(stack, origin, tmp_path / "profile")
            async with editor.tab() as page:
                stack.push_async_callback(page.context.close)
                await page.context.route("**/*", serve)
            agent = await stack.enter_async_context(Client(make_server(editor)))

            async def call(name, **args):
                result = await agent.call_tool(name, args)
                assert not result.is_error, result.content
                return result.structured_content

            assert (await call("open_miminet"))["status"] == "login_required"
            denied = await agent.call_tool("create_network", {})
            assert denied.is_error and denied.content[0].type == "text"
            assert "signing in" in denied.content[0].text
            await page.evaluate("""() => fetch('/auth/login.html?next=home', {method: 'POST',
                body: new URLSearchParams({email: 'student@example.test', password: 'test'})})""")
            assert (await call("open_miminet"))["networks"] == []
            denied = await agent.call_tool(
                "add_device", {"guid": other_guid, "device": "host", "x": 50, "y": 50}
            )
            assert denied.is_error and denied.content[0].type == "text"
            assert "belongs to another user" in denied.content[0].text

            title = "Практикум № 1"
            network = await call("create_network", title=title)
            guid = network["guid"]
            devices = {}
            for kind, x, y in (
                ("host", 5, 45),
                ("switch", 35, 25),
                ("router", 50, 45),
                ("hub", 65, 25),
                ("server", 80, 45),
            ):
                added = await call("add_device", guid=guid, device=kind, x=x, y=y)
                devices[kind] = added["device_id"]
            host, switch = devices["host"], devices["switch"]
            edge = await call("connect_devices", guid=guid, source=host, target=switch)
            again = await call("connect_devices", guid=guid, source=switch, target=host)
            assert again["id"] == edge["id"]
            for source, target in (
                ("switch", "router"),
                ("router", "hub"),
                ("hub", "server"),
            ):
                await call(
                    "connect_devices",
                    guid=guid,
                    source=devices[source],
                    target=devices[target],
                )
            saved = await call("get_network", guid=guid)
            by_id = {node["id"]: node for node in saved["devices"]}
            iface = by_id[host]["interfaces"][0]
            assert iface["connect"] == edge["id"]
            args = {"guid": guid, "device_id": host, "interface_id": iface["id"]}
            await call(
                "configure_interface", **args, address="10.0.0.2/24", gateway="10.0.0.1"
            )
            await call("configure_interface", **args, address="10.0.0.3/24")
            for kind, ip in (("router", "10.0.0.1"), ("server", "10.0.1.2")):
                device_id = devices[kind]
                result = await call(
                    "configure_interface",
                    guid=guid,
                    device_id=device_id,
                    interface_id=by_id[device_id]["interfaces"][0]["id"],
                    address=ip + "/24",
                )
                assert result["interface"]["ip"] == ip
            assert (
                await agent.call_tool("configure_interface", {**args, "address": "bad"})
            ).is_error

            listed = (await call("open_miminet"))["networks"]
            assert listed == [{"guid": guid, "title": title, "url": network["url"]}]
            saved = await call("get_network", guid=guid)
            assert len(saved["links"]) == 4
            device = saved["devices"][0]
            assert (
                device["interfaces"][0]["ip"],
                device["interfaces"][0]["netmask"],
                device["gateway"],
            ) == ("10.0.0.3", 24, "10.0.0.1")
            assert await page.evaluate(
                "global_cy.nodes().length === 5 && global_cy.edges().length === 4"
            )
            assert page.url == network["url"]

            async def operate(tool, **args):
                return await call(tool, guid=guid, **args)

            async def add(kind, x, y):
                return (await operate("add_device", device=kind, x=x, y=y))["device_id"]

            router = devices["router"]
            remote = await add("router", 75, 65)
            hacker = await add("hacker", 60, 85)
            note = await add("textbox", 45, 75)
            await operate(
                "configure_device",
                device_id=note,
                label="Учебная сеть\nGRE и VLAN",
                x=50,
                y=65,
                style={"color": "#336699", "tb_fontsize": 18, "width": 160},
            )
            await operate("configure_device", device_id=remote, label="Филиал")
            await operate("connect_devices", source=remote, target=hacker)
            wan = await operate("connect_devices", source=router, target=remote)
            parallel = await operate(
                "connect_devices", source=router, target=remote, parallel=True
            )
            assert wan["id"] != parallel["id"]
            await operate("configure_link", link_id=wan["id"], loss=2)
            await operate("configure_link", link_id=wan["id"], duplicate=3)
            graph = await operate("get_network")

            def ports(id):
                return next(n["interfaces"] for n in graph["devices"] if n["id"] == id)

            def port(id, edge_id):
                return next(i["id"] for i in ports(id) if i["connect"] == edge_id)

            switch_ports = ports(switch)
            await operate(
                "configure_switch",
                device_id=switch,
                mode="rstp",
                priority=4096,
                ports=[
                    {
                        "interface_id": switch_ports[0]["id"],
                        "mode": "access",
                        "vlans": [10],
                    },
                    {
                        "interface_id": switch_ports[1]["id"],
                        "mode": "trunk",
                        "vlans": [10, 20],
                    },
                ],
            )
            for id, ip in [(router, "192.0.2.1"), (remote, "192.0.2.2")]:
                await operate(
                    "configure_interface",
                    device_id=id,
                    interface_id=port(id, wan["id"]),
                    address=ip + "/30",
                )
            await operate(
                "configure_interface",
                device_id=hacker,
                interface_id=ports(hacker)[0]["id"],
                address="10.1.0.2/24",
            )
            options = await operate("list_device_actions", device_id=router)
            assert {"gre", "ipip", "nat", "static_route", "vlan_subinterface"} <= {
                a["action"] for a in options["actions"]
            }
            assert (await operate("list_device_actions", device_id=note))[
                "actions"
            ] == []
            expected = {}
            for id, local, far, inner in [
                (router, "192.0.2.1", "192.0.2.2", "10.10.0.1"),
                (remote, "192.0.2.2", "192.0.2.1", "10.10.0.2"),
            ]:
                for action, name in [("gre", "gre1"), ("ipip", "tun1")]:
                    params = {
                        "local_ip": local,
                        "remote_ip": far,
                        "tunnel_ip": inner
                        if action == "gre"
                        else inner.replace("10.10.", "10.11."),
                        "name": name,
                    }
                    result = await operate(
                        "set_device_action",
                        device_id=id,
                        action=action,
                        parameters=params,
                    )
                    assert result["parameters"] == params
                    expected[result["id"]] = params
            await operate(
                "configure_vxlan",
                device_id=router,
                interface_id=ports(router)[0]["id"],
                role="client",
                vni=5001,
            )
            await operate(
                "configure_vxlan",
                device_id=router,
                interface_id=port(router, wan["id"]),
                role="network",
                peers=[
                    {"vni": 5001, "remote_ip": "192.0.2.2"},
                    {"vni": 5002, "remote_ip": "192.0.2.2"},
                ],
            )
            route = {"device_id": router, "action": "static_route"}
            route_params = {
                "network": "10.20.0.0",
                "prefix": 24,
                "gateway": "10.10.0.2",
            }
            old = await operate("set_device_action", **route, parameters=route_params)
            new = await operate(
                "set_device_action",
                **route,
                parameters={**route_params, "prefix": 25},
                action_id=old["id"],
            )
            await operate("remove_device_action", action_id=new["id"])

            invalid = await agent.call_tool(
                "set_device_action",
                {
                    "guid": guid,
                    "device_id": host,
                    "action": "gre",
                    "parameters": {
                        "local_ip": "10.0.0.3",
                        "remote_ip": "192.0.2.2",
                        "tunnel_ip": "10.1.2.1",
                        "name": "gre1",
                    },
                },
            )
            assert invalid.is_error
            # Exercise every ordinary command through the matching Flask configurator.
            ri = port(router, wan["id"])
            si = ports(devices["server"])[0]["id"]
            cases = [
                (hacker, "ping", {"ip": "10.1.0.1"}),
                (host, "ping_options", {"options": "-c 2", "ip": "10.0.0.1"}),
                (host, "traceroute", {"options": "-n", "ip": "10.0.0.1"}),
                (host, "static_arp", {"ip": "10.0.0.1", "mac": "02:00:00:00:00:01"}),
                (host, "dhcp_client", {"interface_id": iface["id"]}),
                (
                    router,
                    "secondary_address",
                    {"interface_id": ri, "ip": "192.0.2.10", "prefix": 28},
                ),
                (router, "static_route", route_params),
                (
                    router,
                    "vlan_subinterface",
                    {"interface_id": ri, "vlan": 10, "ip": "10.20.0.1", "prefix": 24},
                ),
                (router, "proxy_arp", {"interface_id": ri}),
                (router, "nat", {"interface_id": ri}),
                (switch, "link_down", {"interface_id": switch_ports[1]["id"]}),
                (switch, "wait", {"seconds": 1}),
                (devices["server"], "block_port", {"port": 443}),
                (
                    devices["server"],
                    "dhcp_server",
                    {
                        "first_ip": "10.0.1.10",
                        "last_ip": "10.0.1.20",
                        "prefix": 24,
                        "gateway": "10.0.1.1",
                        "interface_id": si,
                    },
                ),
            ]
            for protocol in ("tcp", "udp"):
                cases.extend(
                    [
                        (
                            host,
                            "send_" + protocol,
                            {"ip": "10.0.1.2", "port": 8000, "size": 16},
                        ),
                        (
                            devices["server"],
                            "listen_" + protocol,
                            {"ip": "10.0.1.2", "port": 8000},
                        ),
                        (
                            router,
                            "forward_" + protocol,
                            {
                                "interface_id": ri,
                                "port": 8001,
                                "destination_ip": "10.0.1.2",
                                "destination_port": 8000,
                            },
                        ),
                    ]
                )
            assert {action for _, action, _ in cases} | {"ipip", "gre"} == set(ACTIONS)
            for device_id, action, params in cases:
                result = await operate(
                    "set_device_action",
                    device_id=device_id,
                    action=action,
                    parameters=params,
                )
                assert {k: str(v).strip() for k, v in result["parameters"].items()} == {
                    k: str(v) for k, v in params.items()
                }
                expected[result["id"]] = result["parameters"]

            graph = await operate("get_network")
            assert {a["id"]: a["parameters"] for a in graph["actions"]} == expected
            sw = next(n for n in graph["devices"] if n["id"] == switch)
            assert (sw["config"]["stp"], sw["config"]["priority"]) == (2, 4096)
            assert [i["vlan"] for i in ports(switch)] == [10, [10, 20]]
            assert ports(router)[0]["vxlan_vni"] == 5001
            uplink = next(i for i in ports(router) if i["connect"] == wan["id"])
            assert uplink["vxlan_vni_to_target_ip"] == [
                ["5001", "192.0.2.2"],
                ["5002", "192.0.2.2"],
            ]
            link = next(e for e in graph["links"] if e["id"] == wan["id"])
            assert (link["loss_percentage"], link["duplicate_percentage"]) == (2, 3)
            assert (
                next(n for n in graph["devices"] if n["id"] == note)["config"]["color"]
                == "#336699"
            )
            assert (
                await page.evaluate(
                    "id => global_cy.getElementById(id).data('label')", remote
                )
                == "Филиал"
            )
            await operate(
                "configure_network",
                title="Филиалы",
                description="Два маршрутизатора",
                zoom=1.5,
                pan_x=20,
                pan_y=-10,
            )
            await operate("configure_device", device_id=host, gateway="")
            await operate(
                "configure_vxlan",
                device_id=router,
                interface_id=uplink["id"],
                role="off",
            )
            await operate("configure_switch", device_id=switch, ports=[])
            removed = await operate("remove_element", element_id=wan["id"])
            assert removed["links"] == [wan["id"]] and len(removed["actions"]) >= 4
            removed = await operate("remove_element", element_id=remote)
            assert parallel["id"] in removed["links"]
            graph = await operate("get_network")
            assert (
                graph["title"],
                graph["description"],
                graph["zoom"],
                graph["pan_x"],
                graph["pan_y"],
            ) == ("Филиалы", "Два маршрутизатора", 1.5, 20, -10)
            assert (
                next(n for n in graph["devices"] if n["id"] == host)["gateway"] is None
            )
            assert not any(n["id"] == remote for n in graph["devices"])
            assert all(i["vlan"] is None for i in ports(switch))
            assert any(j["action"] == "dhcp_server" for j in graph["actions"])
            await create_example(call)

    asyncio.run(check())
