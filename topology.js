// Evaluated in the signed-in editor. Only the MCP server selects the operation.
async ({guid, operation, catalog, ...p}) => {
    if (network_guid !== guid) throw Error('The editor changed.');
    const node = nodes.find(n => n.data.id === p.device_id);
    const edge = edges.find(e => e.data.id === p.link_id);
    const type = node?.config.type;
    const formType = ({hacker: 'host_hacker', l2_switch: 'switch', l1_hub: 'hub'})[type] || type;
    const paths = {host: '/host/save_config', host_hacker: '/host_hacker/save_config',
        router: '/host/router_save_config', switch: '/host/switch_save_config', server: '/host/server_save_config'};

    async function post(path, data, json = false) {
        try {
            return await ajaxWithAuth({type: 'POST', url: ExternalUrlFor(path), dataType: 'json',
                ...(json ? {contentType: 'application/json', data: JSON.stringify(data)} : {data})});
        } catch {
            throw Error('Save was not confirmed. Read the network before retrying.');
        }
    }
    function redraw(stale) {
        if (stale) SetNetworkPlayerState(-1);
        DrawGraph();
        TakeGraphPictureAndUpdate();
    }
    const form = {net_guid: guid, [formType + '_id']: p.device_id};
    function describe(job) {
        const entry = Object.entries(catalog).find(([, [id]]) => id === job.job_id);
        const result = {id: job.id, device_id: job.host_id, level: job.level, action: entry?.[0] || 'manual'};
        if (entry) result.parameters = Object.fromEntries(
            Object.keys(entry[1][1]).map((key, i) => [key, job['arg_' + (i + 1)]]));
        return result;
    }
    const view = () => ({title: document.querySelector('#network_title').value,
        description: document.querySelector('#network_description').value,
        zoom: global_cy.zoom(), pan_x: global_cy.pan().x, pan_y: global_cy.pan().y});
    if (operation === 'settings') {
        const settings = view();
        for (const key of Object.keys(settings)) if (p[key] != null) settings[key] = p[key];
        const {title, description, zoom, pan_x, pan_y} = settings;
        const saved = await post('/network/update_network_config?guid=' + guid, {
            network_title: title, network_description: description, zoom, pan_x, pan_y}, true);
        if (saved.message !== 'Done') throw Error('Miminet did not confirm network settings.');
        return {guid, url: location.href, ...settings};
    }
    if (operation === 'read') return {
        guid, url: location.href, ...view(),
        devices: nodes.map(n => ({id: n.data.id, label: n.data.label, type: n.config.type,
            position: n.position, gateway: n.config.default_gw || null, interfaces: n.interface, config: n.config})),
        links: edges.map(e => e.data), actions: jobs.map(describe),
    };
    if (operation === 'link') {
        if (!edge) throw Error('Unknown link ID.');
        const data = edge.data;
        const loss = p.loss ?? data.loss_percentage ?? 0;
        const duplicate = p.duplicate ?? data.duplicate_percentage ?? 0;
        if (loss !== (data.loss_percentage ?? 0) || duplicate !== (data.duplicate_percentage ?? 0)) {
            const saved = await post('/edge/save_config', {net_guid: guid, edge_id: p.link_id,
                edge_loss: loss, edge_duplicate: duplicate});
            if (!saved.edges?.some(e => e.data.id === p.link_id &&
                e.data.loss_percentage === loss && e.data.duplicate_percentage === duplicate))
                throw Error('Miminet did not confirm the link settings.');
            edges = saved.edges;
            redraw(true);
        }
        return edges.find(e => e.data.id === p.link_id).data;
    }
    if (operation === 'remove_action') {
        if (!jobs.some(j => j.id === p.action_id)) throw Error('Unknown action ID.');
        const saved = await post('/host/delete_job', {guid, id: p.action_id});
        if (!Array.isArray(saved.jobs) || saved.jobs.some(j => j.id === p.action_id))
            throw Error('Action removal was not confirmed.');
        jobs = saved.jobs;
        redraw(true);
        return {removed: p.action_id};
    }
    if (operation === 'remove_element') {
        const removedNode = nodes.find(n => n.data.id === p.element_id);
        const removedLinks = edges.filter(({data: e}) => removedNode
            ? e.source === p.element_id || e.target === p.element_id : e.id === p.element_id);
        if (!removedNode && !removedLinks.length) throw Error('Unknown node or link ID.');
        const links = new Set(removedLinks.map(e => e.data.id));
        const removedJobs = jobs.filter(j => {
            if (j.host_id === removedNode?.data.id) return true;
            const params = describe(j).parameters;
            const device = nodes.find(n => n.data.id === j.host_id);
            const ports = device?.interface || [];
            const lost = ports.filter(i => links.has(i.connect));
            return lost.some(i => params?.interface_id === i.id ||
                (i.ip && params?.local_ip === i.ip && !ports.some(r => !links.has(r.connect) && r.ip === i.ip)) ||
                Object.entries(j).some(([key, value]) => key.startsWith('arg_') && value === i.id));
        });
        const keptJobs = jobs.filter(j => !removedJobs.includes(j));
        const previousNodes = nodes, previousEdges = edges;
        let nextNodes, nextEdges;
        try {
            nodes = structuredClone(nodes);
            edges = structuredClone(edges);
            if (removedNode) DeleteNode(p.element_id);
            else DeleteEdge(p.element_id);
            nextNodes = nodes;
            nextEdges = edges;
        } finally {
            nodes = previousNodes;
            edges = previousEdges;
        }
        const saved = await post('/post_nodes_edges?guid=' + guid, [nextNodes, nextEdges, keptJobs], true);
        if (saved.code !== 'SUCCESS') throw Error('Miminet did not confirm removal.');
        nodes = nextNodes;
        edges = nextEdges;
        jobs = keptJobs;
        redraw(removedNode?.config.type !== 'textbox');
        return {removed: p.element_id, links: [...links], actions: removedJobs.map(j => j.id)};
    }
    if (!node) throw Error('Select a device ID from get_network.');
    if (operation === 'actions' || operation === 'set_action') {
        const template = document.getElementById('config_' + formType + '_job_script');
        if (!template && Object.hasOwn(paths, formType)) throw Error('Device settings did not load. Reopen the editor.');
        const html = new DOMParser().parseFromString(template?.textContent || '', 'text/html');
        const offered = new Map([...html.querySelectorAll('select option')].map(o => [Number(o.value), o.textContent.trim()]));
        const available = Object.entries(catalog).filter(([, [id]]) => offered.has(id));
        if (operation === 'actions') return {device_id: p.device_id, actions: available.map(([action, [id, fields]]) =>
            ({action, label: offered.get(id), parameters: Object.keys(fields)}))};
        const match = available.find(([name]) => name === p.action);
        if (!match) throw Error('This device does not offer that action.');
        const [jobId, fields] = match[1];
        const old = p.action_id && jobs.find(j => j.id === p.action_id && j.host_id === p.device_id && j.job_id === jobId);
        if (p.action_id && !old) throw Error('Select an existing action of this kind on this device.');
        const iface = node.interface.find(i => i.id === p.values.interface_id);
        if (p.values.interface_id && !iface) throw Error('The selected interface does not belong to this device.');
        if (p.values.local_ip && !node.interface.some(i => i.ip === p.values.local_ip))
            throw Error('Assign the local tunnel endpoint IP to a physical router interface first.');
        if (p.action === 'wait' && Number(p.values.seconds) + jobs.filter(j => j.job_id === 7 && j.id !== p.action_id)
            .reduce((sum, j) => sum + Number(j.arg_1), 0) > 60) throw Error('Miminet allows 60 seconds total wait time.');
        if (old && Object.keys(fields).every((key, i) => String(old['arg_' + (i + 1)]) === p.values[key])) return describe(old);

        const prefix = 'config_' + formType + '_';
        form[prefix + 'job_select_field'] = jobId;
        for (const [key, field] of Object.entries(fields)) form[prefix + field] = p.values[key];
        if (['proxy_arp', 'link_down'].includes(p.action)) {
            const link = edges.find(e => e.data.id === iface.connect)?.data;
            const peer = link && nodes.find(n => n.data.id === (link.source === p.device_id ? link.target : link.source));
            if (!peer) throw Error('The selected interface has no peer.');
            form[formType + '_connection_host_label_hidden'] = peer.data.label;
        }
        if (old) form.editing_job_id = p.action_id;
        // The device configurators reset these fields when they are absent.
        form[prefix + 'default_gw'] = node.config.default_gw || '';
        form.config_rstp_stp = node.config.stp || 0;
        const before = new Set(jobs.map(j => j.id));
        const saved = await post(paths[formType], form);
        if (saved.warning) throw Error(saved.warning);
        const added = saved.jobs?.find(j => !before.has(j.id) && j.host_id === p.device_id && j.job_id === jobId);
        if (!added || !Array.isArray(saved.nodes)) throw Error('Miminet did not confirm the device action.');
        nodes = saved.nodes;
        jobs = saved.jobs;
        redraw(true);
        return describe(added);
    }

    // Stage on a copy. A rejected edit leaves the graph in the browser intact.
    const updated = structuredClone(node);
    let path = '/post_network_nodes';
    if (operation === 'device') {
        if (p.gateway != null) {
            if (!['host', 'hacker', 'server', 'router'].includes(type))
                throw Error('Choose an IP-capable device to set the gateway.');
            updated.config.default_gw = p.gateway;
        }
        if (p.label != null) {
            if (type !== 'textbox' && (p.label.length > 32 || /[\r\n]/.test(p.label)))
                throw Error('Device names are single-line and up to 32 characters.');
            updated.config.label = updated.data.label = p.label;
        }
        if (p.style) {
            if (type !== 'textbox') throw Error('Text styles apply to textboxes.');
            Object.assign(updated.config, p.style);
        }
        if (p.x != null) {
            const cy = global_cy, pan = cy.pan(), zoom = cy.zoom();
            updated.position = {x: (cy.width() * p.x / 100 - pan.x) / zoom,
                y: (cy.height() * p.y / 100 - pan.y) / zoom};
        }
        if (p.label == null && !p.style && p.gateway == null) path = '/move_network_nodes';
    } else if (operation === 'switch') {
        if (type !== 'l2_switch') throw Error('Select a switch.');
        if (p.mode != null) updated.config.stp = {off: 0, stp: 1, rstp: 2}[p.mode];
        if (p.priority != null) updated.config.priority = p.priority;
        if (p.ports?.length === 0) resetInterfaceFields(updated);
        if (p.ports?.length) {
            const ids = new Set();
            for (const spec of p.ports) {
                const port = updated.interface.find(i => i.id === spec.interface_id);
                if (!port || ids.has(port.id)) throw Error('Select distinct ports belonging to this switch.');
                ids.add(port.id);
                port.type_connection = spec.mode === 'access' ? 0 : 1;
                port.vlan = spec.mode === 'access' ? spec.vlans[0] : spec.vlans;
            }
            // Enabling VLANs in the editor gives untouched ports access VLAN 1.
            for (const i of updated.interface) {
                i.type_connection ??= 0;
                i.vlan ??= 1;
            }
        }
    } else if (operation === 'vxlan') {
        if (type !== 'router') throw Error('Select a router to configure a VTEP.');
        const port = updated.interface.find(i => i.id === p.interface_id);
        if (!port) throw Error('Select an interface on this router.');
        const seen = new Set(updated.interface.filter(i => i.id !== port.id)
            .flatMap(i => (i.vxlan_vni_to_target_ip || []).map(([v, ip]) => v + ':' + ip)));
        const peers = p.peers.map(({vni, remote_ip}) => {
            const key = vni + ':' + remote_ip;
            if (seen.has(key)) throw Error('This VNI and remote VTEP pair already exists.');
            seen.add(key);
            return [String(vni), remote_ip];
        });
        port.vxlan_connection_type = {off: null, client: 0, network: 1}[p.role];
        port.vxlan_vni = p.vni;
        port.vxlan_vni_to_target_ip = p.role === 'network' ? peers : null;
    } else throw Error('Unknown editor operation.');

    if (JSON.stringify(node) !== JSON.stringify(updated)) {
        const next = nodes.map(n => n === node ? updated : n);
        const graph = operation === 'switch' || operation === 'vxlan';
        if (graph) path = '/post_nodes_edges';
        const saved = await post(path + '?guid=' + guid, graph ? [next, edges, jobs] : next, true);
        if (saved.code !== 'SUCCESS') throw Error('Miminet did not confirm the updated device.');
        nodes = next;
        redraw(path !== '/move_network_nodes' && type !== 'textbox');
    }
    return {device_id: updated.data.id, config: updated.config, interfaces: updated.interface, position: updated.position};
}
