async ([guid, source, target, parallel]) => {
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
}
