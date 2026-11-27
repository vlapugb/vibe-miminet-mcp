"""MCP tools for reading and editing Miminet networks."""

from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from mcp.server.mcpserver.exceptions import ToolError
from playwright.async_api import Error, Page
from pydantic import BaseModel, ConfigDict, Field

from .actions import ACTIONS, action_parameters
from .browser import LOGIN_MESSAGE, EditorSession

SCRIPTS = Path(__file__).with_name("scripts")
TOPOLOGY = (SCRIPTS / "topology.js").read_text(encoding="utf-8")
ADD_DEVICE = (SCRIPTS / "add_device.js").read_text(encoding="utf-8")
CONNECT_DEVICES = (SCRIPTS / "connect_devices.js").read_text(encoding="utf-8")
CONFIGURE_INTERFACE = (SCRIPTS / "configure_interface.js").read_text(encoding="utf-8")

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


def network_title(title: str | None) -> str | None:
    if title is not None:
        title = title.strip()
        if not title or not title.isprintable() or "\\" in title:
            raise ToolError("Use a single-line network title without backslashes.")
    return title


class Editor(EditorSession):
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
                    ADD_DEVICE,
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
                CONNECT_DEVICES,
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
                CONFIGURE_INTERFACE,
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
