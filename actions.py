"""Map named device actions to the Miminet form fields validated by Flask."""

import re
from collections.abc import Mapping
from ipaddress import IPv4Address

from mcp.server.mcpserver.exceptions import ToolError

# Fields follow the configurator's arg_1, arg_2, ... order for reading saved jobs.
ACTIONS = {
    "ping": (1, {"ip": "ping_c_1_ip"}),
    "ping_options": (
        2,
        {
            "options": "ping_with_options_options_input_field",
            "ip": "ping_with_options_ip_input_field",
        },
    ),
    "traceroute": (
        5,
        {
            "options": "traceroute_with_options_options_input_field",
            "ip": "traceroute_with_options_ip_input_field",
        },
    ),
    "link_down": (6, {"interface_id": "link_down_iface_select_field"}),
    "wait": (7, {"seconds": "sleep"}),
    "secondary_address": (
        100,
        {
            "interface_id": "add_ip_mask_iface_select_field",
            "ip": "add_ip_mask_ip_input_field",
            "prefix": "add_ip_mask_mask_input_field",
        },
    ),
    "nat": (101, {"interface_id": "add_nat_masquerade_iface_select_field"}),
    "static_route": (
        102,
        {
            "network": "add_route_ip_input_field",
            "prefix": "add_route_mask_input_field",
            "gateway": "add_route_gw_input_field",
        },
    ),
    "static_arp": (
        103,
        {"ip": "add_arp_cache_ip_input_field", "mac": "add_arp_cache_mac_input_field"},
    ),
    "vlan_subinterface": (
        104,
        {
            "interface_id": "add_subinterface_iface_select_field",
            "ip": "add_subinterface_ip_input_field",
            "prefix": "add_subinterface_mask_input_field",
            "vlan": "add_subinterface_vlan_input_field",
        },
    ),
    "ipip": (
        105,
        {
            "local_ip": "add_ipip_tunnel_iface_select_ip_field",
            "remote_ip": "add_ipip_tunnel_end_ip_input_field",
            "tunnel_ip": "add_ipip_tunnel_interface_ip_input_field",
            "name": "add_ipip_tunnel_interface_name_field",
        },
    ),
    "gre": (
        106,
        {
            "local_ip": "add_gre_interface_select_ip_field",
            "remote_ip": "add_gre_interface_end_ip_input_field",
            "tunnel_ip": "add_gre_interface_ip_input_field",
            "name": "add_gre_interface_name_field",
        },
    ),
    "proxy_arp": (107, {"interface_id": "add_arp_proxy_iface_select_field"}),
    "dhcp_client": (108, {"interface_id": "add_dhclient_interface_select_iface_field"}),
    "block_port": (202, {"port": "block_tcp_udp_port_input_field"}),
    "dhcp_server": (
        203,
        {
            "first_ip": "add_dhcp_ip_range_1_input_field",
            "last_ip": "add_dhcp_ip_range_2_input_field",
            "prefix": "add_dhcp_mask_input_field",
            "gateway": "add_dhcp_gateway_input_field",
            "interface_id": "add_dhcp_interface_select_iface_field",
        },
    ),
}
for protocol, send, listen, forward in (("udp", 3, 200, 110), ("tcp", 4, 201, 109)):
    ACTIONS["send_" + protocol] = (
        send,
        {
            "size": f"send_{protocol}_data_size_input_field",
            "ip": f"send_{protocol}_data_ip_input_field",
            "port": f"send_{protocol}_data_port_input_field",
        },
    )
    ACTIONS["listen_" + protocol] = (
        listen,
        {
            "ip": f"start_{protocol}_server_ip_input_field",
            "port": f"start_{protocol}_server_port_input_field",
        },
    )
    field = f"add_port_forwarding_{protocol}_"
    ACTIONS["forward_" + protocol] = (
        forward,
        {
            "interface_id": field + "iface_select_field",
            "port": field + "port_input_field",
            "destination_ip": field + "dest_ip_input_field",
            "destination_port": field + "dest_port_input_field",
        },
    )

BOUNDS = {
    "seconds": (1, 50),
    "prefix": (0, 32),
    "vlan": (1, 4094),
    "size": (0, 65535),
    "port": (0, 65535),
    "destination_port": (0, 65535),
}


def action_parameters(
    action: str, parameters: Mapping[str, str | int]
) -> dict[str, str]:
    if action not in ACTIONS:
        raise ToolError("Choose an action returned by list_device_actions.")
    fields = ACTIONS[action][1]
    if parameters.keys() != fields.keys():
        raise ToolError(
            f"{action} requires exactly these parameters: {', '.join(fields)}."
        )
    values = {}
    for key in fields:
        value = str(parameters[key])
        if key in BOUNDS:
            low, high = BOUNDS[key]
            if not re.fullmatch(r"[0-9]{1,5}", value) or not low <= int(value) <= high:
                raise ToolError(f"{key} must be an integer between {low} and {high}.")
            value = str(int(value))
        elif key == "name":
            if not re.fullmatch(r"[a-z][a-z0-9_-]{1,14}", value):
                raise ToolError(
                    "Use a 2–15 character lowercase tunnel name starting with a letter."
                )
        elif key == "mac":
            if not re.fullmatch(
                r"[\da-fA-F]{2}([:-]?)(?:[\da-fA-F]{2}\1){4}[\da-fA-F]{2}",
                value,
                re.ASCII,
            ):
                raise ToolError("Invalid MAC address.")
        elif key == "options":
            # The editor accepts option strings. Forward only complete supported flags.
            flag = (
                r"-(?:c (?:[1-9]|10)|[ti] [1-9][0-9]{0,2}|s [0-9]{1,4})"
                if action == "ping_options"
                else r"-(?:[Fn]|[ifgmp] [0-9]{1,5})"
            )
            if not re.fullmatch(rf"{flag}(?: {flag})*", value):
                raise ToolError("Use supported numeric ping or traceroute options.")
        elif key == "interface_id":
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", value):
                raise ToolError("Use an interface ID from get_network.")
        else:
            try:
                value = str(IPv4Address(value))
            except ValueError as exc:
                raise ToolError(f"{key} must be an IPv4 address.") from exc
        values[key] = value
    return values
