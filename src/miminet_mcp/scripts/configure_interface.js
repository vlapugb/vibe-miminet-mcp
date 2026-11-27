async ({guid, device, iface, ip, prefix, gateway, paths}) => {
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
}
