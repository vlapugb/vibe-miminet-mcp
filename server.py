"""Control a signed-in Miminet editor through MCP over stdio."""

import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Error,
    Page,
    async_playwright,
)
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from actions import ACTIONS, action_parameters

TOPOLOGY = Path(__file__).with_name("topology.js").read_text()
Device = Literal["host", "hacker", "router", "switch", "hub", "server", "textbox"]
Vni = Annotated[int, Field(ge=1, le=16777214)]
Vlan = Annotated[int, Field(ge=1, le=4094)]
Percentage = Annotated[int, Field(ge=0, le=100)]


class SwitchPort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interface_id: str
    mode: Literal["access", "trunk"]
    vlans: Annotated[list[Vlan], Field(min_length=1)]


class VtepPeer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vni: Vni
    remote_ip: IPv4Address


class TextStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tb_fontsize: Annotated[int, Field(ge=8, le=120)] | None = None
    color: Annotated[str, Field(pattern=r"^#[a-fA-F0-9]{6}$")] | None = None
    fontstyle: Literal["normal", "italic"] | None = None
    fontweight: Literal["normal", "bold"] | None = None
    width: Annotated[int, Field(ge=30)] | None = None
    height: Annotated[int, Field(ge=30)] | None = None


DEVICE_ICONS = {
    "host": "host",
    "hacker": "hacker",
    "textbox": "textbox",
    "router": "l3_router",
    "switch": "l2_switch",
    "hub": "l1_hub",
    "server": "server",
}
IP_CONFIG = {
    "host": "/host/save_config",
    "hacker": "/host_hacker/save_config",
    "router": "/host/router_save_config",
    "server": "/host/server_save_config",
}
LOGIN_MESSAGE = "Finish signing in yourself in the Miminet browser window, then retry."


def network_title(title: str | None) -> str | None:
    if title is not None:
        title = title.strip()
        if not title or not title.isprintable() or "\\" in title:
            raise ToolError("Use a single-line network title without backslashes.")
    return title


class Editor:
    def __init__(self, resources: AsyncExitStack, origin: str, profile: Path):
        url = AnyHttpUrl(origin)
        if (
            (
                url.scheme == "http"
                and url.host not in {"localhost", "127.0.0.1", "[::1]"}
            )
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "MIMINET_URL must be an HTTPS origin or a loopback HTTP origin."
            )
        self.origin = str(url).rstrip("/")
        self.profile = profile
        self.resources = resources
        self.firefox: BrowserType | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.lock = asyncio.Lock()

    def _closed(self, _context):
        self.context = None
        self.page = None

    @asynccontextmanager
    async def tab(self, guid: UUID | None = None):
        async with self.lock:
            try:
                if self.context is None:
                    if self.firefox is None:
                        driver = await self.resources.enter_async_context(
                            async_playwright()
                        )
                        self.firefox = driver.firefox
                    self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                    self.context = await self.firefox.launch_persistent_context(
                        self.profile, headless=False
                    )
                    self.context.on("close", self._closed)
                if self.page is None or self.page.is_closed():
                    self.page = (
                        next(iter(self.context.pages), None)
                        or await self.context.new_page()
                    )
                page = self.page
                await page.bring_to_front()
                if guid is not None:
                    # The editor saves complete lists of nodes and edges. Read the latest
                    # saved copy before changing either, including edits from other tabs.
                    await self.navigate(page, f"/web_network?guid={guid}")
                    await self.check_editor(page, guid)
                yield page
            except (Error, OSError) as exc:
                if self.context is None:
                    message = (
                        "Cannot open Firefox. Check the browser installation, desktop display "
                        "and that this profile is writable and free."
                    )
                elif isinstance(exc, Error) and exc.message.startswith(
                    "Page.evaluate: "
                ):
                    message = (
                        exc.message.splitlines()[0]
                        .removeprefix("Page.evaluate: ")
                        .removeprefix("Error: ")
                    )
                else:
                    message = (
                        "The browser did not finish the operation. Check the Miminet window "
                        "and read the network before retrying."
                    )
                raise ToolError(message) from exc

    async def navigate(self, page: Page, path: str):
        if page.url.startswith(self.origin + "/web_network?"):
            await page.wait_for_function("!window.jQuery || jQuery.active === 0")
        response = await page.goto(self.origin + path, wait_until="domcontentloaded")
        if response is None or not response.ok:
            raise ToolError(f"Miminet did not open {path.split('?')[0]} successfully.")

    async def home(self, page: Page) -> bool:
        await self.navigate(page, "/home")
        if page.url != self.origin + "/home":
            if (
                urlsplit(page.url)._replace(query="", fragment="").geturl()
                == self.origin + "/auth/login.html"
            ):
                return False
            raise ToolError("Miminet redirected away from the network list.")
        await page.locator("#new-network-button").wait_for(state="attached")
        return True

    async def check_editor(self, page: Page, guid: UUID):
        if page.url != f"{self.origin}/web_network?guid={guid}":
            if page.url.split("?")[0] == self.origin + "/auth/login.html":
                raise ToolError(LOGIN_MESSAGE)
            raise ToolError("This network is missing or belongs to another user.")
        await page.wait_for_function(
            "guid => typeof network_guid !== 'undefined' && network_guid === guid"
            " && typeof global_cy !== 'undefined' && global_cy",
            arg=str(guid),
        )

    async def open_miminet(self) -> dict[str, object]:
        """Open the visible login or My Networks page. Return the signed-in user's networks."""
        async with self.tab() as page:
            if not await self.home(page):
                return {"status": "login_required", "message": LOGIN_MESSAGE}
            networks = await page.locator('a[href^="/web_network?guid="]').evaluate_all(
                """links => links.map(a => ({
                    guid: new URL(a.href).searchParams.get('guid'),
                    title: a.textContent.trim(), url: a.href
                }))"""
            )
            return {"status": "signed_in", "networks": networks}

    async def create_network(self, title: str | None = None) -> dict[str, object]:
        """Create one empty network under the signed-in account and show its editor."""
        title = network_title(title)
        async with self.tab() as page:
            if not await self.home(page):
                raise ToolError(LOGIN_MESSAGE)
            await self.navigate(page, "/create_network")
            values = parse_qs(urlsplit(page.url).query).get("guid", [])
            if not values:
                raise ToolError(
                    "Creation was not confirmed. Check My Networks before retrying."
                )
            guid = UUID(values[0])
            await self.check_editor(page, guid)
            if title is not None:
                try:
                    await self.save_settings(page, guid, title=title)
                except (Error, ToolError) as exc:
                    raise ToolError(
                        f"Network {guid} was created. Check its title at {self.origin}/web_network?guid={guid}."
                    ) from exc
            return {"guid": str(guid), "url": page.url}

    async def save_settings(
        self, page: Page, guid: UUID, **settings
    ) -> dict[str, object]:
        saved = await page.evaluate(
            TOPOLOGY, {"guid": str(guid), "operation": "settings", **settings}
        )
        await self.navigate(page, f"/web_network?guid={guid}")
        await self.check_editor(page, guid)
        return saved

    async def configure_network(
        self,
        guid: UUID,
        title: str | None = None,
        description: str | None = None,
        zoom: Annotated[float, Field(ge=0.5, le=2)] | None = None,
        pan_x: int | None = None,
        pan_y: int | None = None,
    ) -> dict[str, object]:
        """Update the title, description or saved viewport. Omitted values are preserved."""
        title = network_title(title)
        if description is not None:
            description = description.strip()
            if not description:
                raise ToolError("Miminet requires a nonempty replacement description.")
        async with self.tab(guid) as page:
            return await self.save_settings(
                page,
                guid,
                title=title,
                description=description,
                zoom=zoom,
                pan_x=pan_x,
                pan_y=pan_y,
            )

    async def get_network(self, guid: UUID) -> dict[str, object]:
        """Read devices, positions, port settings, links and ordered device actions."""
        return await self.topology(guid, "read", catalog=ACTIONS)

    async def topology(self, guid: UUID, operation: str, **args) -> dict[str, object]:
        async with self.tab(guid) as page:
            if operation in {"actions", "set_action"}:
                await page.wait_for_function("jQuery.active === 0")
            return await page.evaluate(
                TOPOLOGY, {"guid": str(guid), "operation": operation, **args}
            )

    async def list_device_actions(
        self, guid: UUID, device_id: str
    ) -> dict[str, object]:
        """List the device's supported actions and required parameter names from its editor menu."""
        return await self.topology(
            guid, "actions", device_id=device_id, catalog=ACTIONS
        )

    async def set_device_action(
        self,
        guid: UUID,
        device_id: str,
        action: str,
        parameters: dict[str, str | int],
        action_id: str | None = None,
    ) -> dict[str, object]:
        """Save a device action such as gre, ipip, static_route, vlan_subinterface, nat or dhcp_server.
        Use list_device_actions for parameter names. IPs have no slash, prefix is separate.
        local_ip is an assigned physical IP, remote_ip is the other router, tunnel_ip is the inner IP.
        Omit action_id to append, or supply the saved ID to edit an action of the same kind.
        """
        values = action_parameters(action, parameters)
        return await self.topology(
            guid,
            "set_action",
            device_id=device_id,
            action=action,
            values=values,
            action_id=action_id,
            catalog=ACTIONS,
        )

    async def remove_device_action(
        self, guid: UUID, action_id: str
    ) -> dict[str, object]:
        """Remove one saved device action by ID."""
        return await self.topology(guid, "remove_action", action_id=action_id)

    async def remove_element(self, guid: UUID, element_id: str) -> dict[str, object]:
        """Remove a node or link, its attached links and actions that require removed ports."""
        return await self.topology(
            guid, "remove_element", element_id=element_id, catalog=ACTIONS
        )

    async def configure_device(
        self,
        guid: UUID,
        device_id: str,
        label: str | None = None,
        x: Percentage | None = None,
        y: Percentage | None = None,
        style: TextStyle | None = None,
        gateway: IPv4Address | Literal[""] | None = None,
    ) -> dict[str, object]:
        """Rename or move a device (x/y percent), style a textbox, or set a device gateway (empty string clears it)."""
        if label is not None and (
            not label.strip()
            or len(label) > 1024
            or "<" in label
            or any(not c.isprintable() and c != "\n" for c in label)
        ):
            raise ToolError("Use plain text up to 1024 characters without markup.")
        if (x is None) != (y is None):
            raise ToolError("Supply both canvas coordinates.")
        return await self.topology(
            guid,
            "device",
            device_id=device_id,
            label=label,
            x=x,
            y=y,
            gateway=None if gateway is None else str(gateway),
            style=style.model_dump(exclude_none=True) if style else None,
        )

    async def configure_link(
        self,
        guid: UUID,
        link_id: str,
        loss: Percentage | None = None,
        duplicate: Percentage | None = None,
    ) -> dict[str, object]:
        """Set packet loss or duplication percentages (0–100). Omitted values are preserved."""
        return await self.topology(
            guid, "link", link_id=link_id, loss=loss, duplicate=duplicate
        )

    async def configure_switch(
        self,
        guid: UUID,
        device_id: str,
        mode: Literal["off", "stp", "rstp"] | None = None,
        priority: Annotated[int, Field(ge=0, le=65535)] | None = None,
        ports: list[SwitchPort] | None = None,
    ) -> dict[str, object]:
        """Set STP/RSTP, bridge priority and access/trunk VLANs. Supply selected ports to update them.
        An empty ports list disables VLANs switch-wide. Omitted settings are preserved.
        """
        if ports and any(
            len(p.vlans) != len(set(p.vlans))
            or (p.mode == "access" and len(p.vlans) != 1)
            for p in ports
        ):
            raise ToolError("Access takes one VLAN. Trunk VLANs must be distinct.")
        return await self.topology(
            guid,
            "switch",
            device_id=device_id,
            mode=mode,
            priority=priority,
            ports=None if ports is None else [p.model_dump() for p in ports],
        )

    async def configure_vxlan(
        self,
        guid: UUID,
        device_id: str,
        interface_id: str,
        role: Literal["off", "client", "network"],
        vni: Vni | None = None,
        peers: list[VtepPeer] | None = None,
    ) -> dict[str, object]:
        """Replace the VXLAN settings of one router port. For client supply vni, for network supply
        all remote VNI/IP pairs in peers, or use off to clear just this port.
        """
        if (role == "client") != (vni is not None) or (role == "network") != bool(
            peers
        ):
            raise ToolError(
                "Client requires only vni, network requires only nonempty peers, off takes neither."
            )
        return await self.topology(
            guid,
            "vxlan",
            device_id=device_id,
            interface_id=interface_id,
            role=role,
            vni=vni,
            peers=[p.model_dump(mode="json") for p in peers or []],
        )

    async def add_device(
        self, guid: UUID, device: Device, x: Percentage, y: Percentage
    ) -> dict[str, object]:
        """Drag a device onto the canvas at x/y percent. Return its generated Miminet ID."""
        async with self.tab(guid) as page:
            previous = set(await page.evaluate("nodes.map(n => n.data.id)"))
            endpoint = await page.evaluate(
                "guid => new URL(ExternalUrlFor('/post_network_nodes?guid=' + guid), location.href).href",
                str(guid),
            )
            async with page.expect_response(
                lambda r: (
                    r.url == endpoint and r.request.method == "POST" and r.status != 401
                )
            ) as saving:
                # Both palette tabs use this drop handler, including when the tools tab is folded.
                await page.evaluate(
                    """([guid, icon, x, y]) => {
                        if (network_guid !== guid) throw Error('The editor changed.');
                        const canvas = $('#network_scheme'), item = $(document.getElementById(icon));
                        const drop = canvas.droppable('option', 'drop');
                        if (!item.hasClass('drag') || typeof drop !== 'function')
                            throw Error('This palette item is unavailable.');
                        const box = canvas[0].getBoundingClientRect();
                        const position = {left: box.left + scrollX + box.width*x/100,
                                          top: box.top + scrollY + box.height*y/100};
                        drop.call(canvas[0], $.Event('drop'), {draggable: item, position});
                    }""",
                    [str(guid), DEVICE_ICONS[device], x, y],
                )
            response = await saving.value
            if not response.ok or (await response.json()).get("code") != "SUCCESS":
                raise ToolError(
                    "Miminet did not confirm saving the device. Read the network before retrying."
                )
            added = [
                n["data"]["id"]
                for n in response.request.post_data_json
                if n["data"]["id"] not in previous
            ]
            if len(added) != 1:
                raise ToolError(
                    "The canvas changed concurrently. Read the network to identify the new device."
                )
            return {"device_id": added[0], "url": page.url}

    async def connect_devices(
        self, guid: UUID, source: str, target: str, parallel: bool = False
    ) -> dict[str, object]:
        """Connect two device IDs. Reuse their link by default. Set parallel to add another physical link."""
        async with self.tab(guid) as page:
            return await page.evaluate(
                """async ([guid, source, target, parallel]) => {
                    if (network_guid !== guid) throw Error('The editor changed.');
                    const supported = ['host', 'hacker', 'server', 'router', 'l2_switch', 'l1_hub'];
                    if (source === target || ![source, target].every(id =>
                        nodes.some(n => n.data.id === id && supported.includes(n.config.type))))
                        throw Error('Choose two distinct devices from get_network.');
                    const existing = edges.find(({data}) =>
                        (data.source === source && data.target === target) ||
                        (data.source === target && data.target === source));
                    if (existing && !parallel) return existing.data;
                    AddEdge(source, target);
                    const link = edges.at(-1).data;
                    if (![source, target].every(id => nodes.find(n => n.data.id === id)
                        .interface.some(i => i.connect === link.id && i.id))) {
                        RestoreNetworkObject();
                        throw Error('A device has no free port.');
                    }
                    try {
                        const saved = await ajaxWithAuth({
                            type: 'POST', url: ExternalUrlFor('/post_nodes_edges?guid=' + guid),
                            contentType: 'application/json', dataType: 'json',
                            data: JSON.stringify([nodes, edges, jobs])
                        });
                        if (saved.code !== 'SUCCESS') throw Error();
                    } catch {
                        RestoreNetworkObject();
                        throw Error('Link save was not confirmed. Read the network before retrying.');
                    }
                    SetNetworkPlayerState(-1);
                    DrawGraph();
                    TakeGraphPictureAndUpdate();
                    return link;
                }""",
                [str(guid), source, target, parallel],
            )

    async def configure_interface(
        self,
        guid: UUID,
        device_id: str,
        interface_id: str,
        address: IPv4Interface,
        gateway: IPv4Address | None = None,
    ) -> dict[str, object]:
        """Set IPv4/prefix on a host, router or server interface. Omit gateway to preserve it."""
        if address.network.prefixlen == 0:
            raise ToolError("Miminet emulates interface prefixes from /1 to /32.")
        async with self.tab(guid) as page:
            return await page.evaluate(
                """async ({guid, device, iface, ip, prefix, gateway, paths}) => {
                    if (network_guid !== guid) throw Error('The editor changed.');
                    const node = nodes.find(n => n.data.id === device);
                    const port = node?.interface?.find(i => i.id === iface);
                    if (!port || !Object.hasOwn(paths, node.config.type))
                        throw Error('Select an IP-capable device and one of its interface IDs from get_network.');
                    const type = node.config.type === 'hacker' ? 'host_hacker' : node.config.type;
                    const field = 'config_' + type + '_';
                    const previousGateway = node.config.default_gw || '';
                    const nextGateway = gateway ?? previousGateway;
                    if (port.ip === ip && Number(port.netmask) === prefix && nextGateway === previousGateway)
                        return {device_id: device, interface: port, gateway: previousGateway || null};
                    const data = {
                        net_guid: guid, [type + '_id']: device, [field + 'iface_ids[]']: iface,
                        [field + 'ip_' + iface]: ip, [field + 'mask_' + iface]: prefix,
                        [field + 'default_gw']: nextGateway
                    };
                    const saved = await ajaxWithAuth({
                        type: 'POST', url: ExternalUrlFor(paths[node.config.type]), data, dataType: 'json'
                    }).catch(() => { throw Error('Interface save was not confirmed. Read the network before retrying.'); });
                    if (saved.warning || !Array.isArray(saved.nodes))
                        throw Error(saved.warning || 'Miminet did not confirm the interface settings.');
                    nodes = saved.nodes;
                    jobs = saved.jobs;
                    SetNetworkPlayerState(-1);
                    DrawGraph();
                    const updated = nodes.find(n => n.data.id === device);
                    return {device_id: device, interface: updated.interface.find(i => i.id === iface),
                            gateway: updated.config.default_gw || null};
                }""",
                {
                    "guid": str(guid),
                    "device": device_id,
                    "iface": interface_id,
                    "ip": str(address.ip),
                    "prefix": address.network.prefixlen,
                    "gateway": str(gateway) if gateway else None,
                    "paths": IP_CONFIG,
                },
            )


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


async def main():
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


if __name__ == "__main__":
    asyncio.run(main())
