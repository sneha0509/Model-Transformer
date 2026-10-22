(() => {
    const svg = document.getElementById("lineageGraph");
    if (!svg) return;
    const status = document.getElementById("lineageGraphStatus");
    const details = document.getElementById("lineageGraphDetails");
    const zoomLevel = document.getElementById("lineageZoomLevel");
    const search = document.getElementById("lineageGraphSearch");
    const tableFilter = document.getElementById("lineageGraphTable");
    const includeReads = document.getElementById("lineageGraphReads");
    const fitFlow = document.getElementById("lineageFitFlow");
    const clearFlow = document.getElementById("lineageClearFlow");
    const namespace = "http://www.w3.org/2000/svg";
    const width = 236;
    const height = 76;
    const detailZoom = 0.55;
    const minimumZoom = 0.0001;
    const labels = {SemanticModel: "Semantic model", Table: "Source table", Notebook: "Notebook", DataPipeline: "Pipeline"};

    function element(name, attributes = {}, text = "") {
        const result = document.createElementNS(namespace, name);
        Object.entries(attributes).forEach(([key, value]) => result.setAttribute(key, value));
        if (text) result.textContent = text;
        return result;
    }

    let graph;
    try {
        graph = JSON.parse(document.getElementById("lineageGraphData").textContent);
        if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) {
            throw new Error("The saved graph is missing nodes or relationships.");
        }
    } catch (error) {
        status.classList.add("is-error");
        status.textContent = `Could not display lineage: ${error.message} Use Refresh repository & lineage to rebuild it.`;
        return;
    }
    const nodes = new Map(graph.nodes.map(node => [node.id, {...node, visible: true}]));
    const edges = graph.edges;
    if (!nodes.has(graph.root) || edges.some(edge => !nodes.has(edge.source) || !nodes.has(edge.target))) {
        status.classList.add("is-error");
        status.textContent = "The saved lineage graph is incomplete. Use Refresh repository & lineage to rebuild it.";
        return;
    }
    const outgoing = new Map([...nodes.keys()].map(id => [id, []]));
    const incoming = new Map([...nodes.keys()].map(id => [id, []]));
    const incident = new Map([...nodes.keys()].map(id => [id, []]));
    const edgesById = new Map(edges.map(edge => [edge.id, edge]));
    edges.forEach(edge => {
        outgoing.get(edge.source).push(edge);
        incoming.get(edge.target).push(edge);
        incident.get(edge.source).push(edge);
        incident.get(edge.target).push(edge);
    });
    const defs = element("defs");
    const marker = element("marker", {
        id: "lineageArrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
        markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse",
    });
    marker.append(element("path", {d: "M 0 0 L 10 5 L 0 10 z", class: "lineage-arrow"}));
    const flowMarker = element("marker", {
        id: "lineageFlowArrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
        markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse",
    });
    flowMarker.append(element("path", {d: "M 0 0 L 10 5 L 0 10 z", class: "lineage-flow-arrow"}));
    defs.append(marker, flowMarker);
    const viewport = element("g");
    const edgeLayer = element("g");
    const nodeLayer = element("g");
    viewport.append(edgeLayer, nodeLayer);
    let scale = 1;
    let offset = {x: 0, y: 0};
    let selected = null;
    const pathNodes = new Set();
    const pathEdges = new Set();
    let compact = false;
    let frame = null;
    let transformDirty = false;
    const dirtyNodes = new Set();
    const dirtyEdges = new Set();

    function requestRender() {
        if (frame !== null) return;
        frame = requestAnimationFrame(() => {
            frame = null;
            if (transformDirty) {
                viewport.setAttribute("transform", `translate(${offset.x} ${offset.y}) scale(${scale})`);
                const percentage = scale * 100;
                zoomLevel.textContent = `${percentage < 1 ? percentage.toFixed(2) : Math.round(percentage)}%`;
                transformDirty = false;
            }
            dirtyNodes.forEach(node => node.element.setAttribute("transform", `translate(${node.x} ${node.y})`));
            dirtyEdges.forEach(drawEdge);
            dirtyNodes.clear();
            dirtyEdges.clear();
        });
    }

    function transform() {
        const nextCompact = scale < detailZoom;
        if (compact !== nextCompact) {
            compact = nextCompact;
            svg.classList.toggle("is-compact", compact);
            edges.forEach(edge => {
                if (edge.visible) dirtyEdges.add(edge);
            });
        }
        transformDirty = true;
        requestRender();
    }

    function point(event) {
        const bounds = svg.getBoundingClientRect();
        return {x: event.clientX - bounds.left, y: event.clientY - bounds.top};
    }

    function zoom(next, anchor = {x: svg.clientWidth / 2, y: svg.clientHeight / 2}) {
        const clamped = Math.min(3, Math.max(minimumZoom, next));
        const ratio = clamped / scale;
        offset = {x: anchor.x - (anchor.x - offset.x) * ratio, y: anchor.y - (anchor.y - offset.y) * ratio};
        scale = clamped;
        transform();
    }

    function fit(focused = false) {
        const root = nodes.get(graph.root);
        let minX = Infinity;
        let maxX = -Infinity;
        let minY = Infinity;
        let maxY = -Infinity;
        nodes.forEach(node => {
            if (!node.visible || (focused && !pathNodes.has(node.id))) return;
            minX = Math.min(minX, node.x);
            minY = Math.min(minY, node.y);
            maxX = Math.max(maxX, node.x + width);
            maxY = Math.max(maxY, node.y + height);
        });
        if (!Number.isFinite(minX)) return;
        const center = focused ? {x: (minX + maxX) / 2, y: (minY + maxY) / 2}
            : {x: root.x + width / 2, y: root.y + height / 2};
        let extentX = width / 2;
        let extentY = height / 2;
        nodes.forEach(node => {
            if (!node.visible || (focused && !pathNodes.has(node.id))) return;
            extentX = Math.max(extentX, Math.abs(node.x + width / 2 - center.x) + width / 2);
            extentY = Math.max(extentY, Math.abs(node.y + height / 2 - center.y) + height / 2);
        });
        scale = Math.min(1.15, Math.max(minimumZoom, Math.min(
            (svg.clientWidth - 60) / (extentX * 2), (svg.clientHeight - 60) / (extentY * 2)
        )));
        offset = {
            x: svg.clientWidth / 2 - center.x * scale,
            y: svg.clientHeight / 2 - center.y * scale,
        };
        transform();
    }

    function drawEdge(edge) {
        const source = nodes.get(edge.source);
        const target = nodes.get(edge.target);
        const dx = target.x - source.x;
        const dy = target.y - source.y;
        const inset = compact ? Math.min(0.5, 12 / (Math.hypot(dx, dy) || 1))
            : Math.min(0.5, dx ? width / 2 / Math.abs(dx) : Infinity, dy ? height / 2 / Math.abs(dy) : Infinity);
        const x1 = source.x + width / 2 + dx * inset;
        const y1 = source.y + height / 2 + dy * inset;
        const x2 = target.x + width / 2 - dx * inset;
        const y2 = target.y + height / 2 - dy * inset;
        edge.path.setAttribute("d", `M ${x1} ${y1} L ${x2} ${y2}`);
        edge.caption.setAttribute("x", (x1 + x2) / 2);
        const shortLink = !compact && Math.hypot(x2 - x1, y2 - y1) < 80;
        const sideLink = Math.abs(dx) / width >= Math.abs(dy) / height;
        edge.caption.setAttribute("y", shortLink && sideLink
            ? Math.min(source.y, target.y) - 8 : (y1 + y2) / 2 + (shortLink ? 4 : -8));
    }

    function drawPositions(node) {
        if (node) {
            dirtyNodes.add(node);
            incident.get(node.id).forEach(edge => {
                if (edge.visible) dirtyEdges.add(edge);
            });
        } else {
            nodes.forEach(item => {
                if (item.visible) dirtyNodes.add(item);
            });
            edges.forEach(edge => {
                if (edge.visible) dirtyEdges.add(edge);
            });
        }
        requestRender();
    }

    function layout() {
        const root = nodes.get(graph.root);
        root.x = -width / 2;
        root.y = -height / 2;
        const order = [graph.root];
        const branches = new Map([[graph.root, {children: []}]]);
        // A spanning tree groups actual flows while shared nodes and cycle links remain single.
        for (let index = 0; index < order.length; index++) {
            const key = order[index];
            const branch = branches.get(key);
            outgoing.get(key).forEach(edge => {
                if (!edge.visible || branches.has(edge.target)) return;
                branches.set(edge.target, {children: [], parent: key});
                branch.children.push(edge.target);
                order.push(edge.target);
            });
        }
        for (let index = order.length - 1; index >= 0; index--) {
            const branch = branches.get(order[index]);
            branch.weight = Math.max(1, branch.children.reduce((sum, key) => sum + branches.get(key).weight, 0));
        }
        const rootBranch = branches.get(graph.root);
        rootBranch.start = -Math.PI;
        rootBranch.sweep = Math.PI * 2;
        order.forEach(key => {
            const branch = branches.get(key);
            let start = branch.start;
            branch.children.forEach(child => {
                const next = branches.get(child);
                const allocation = branch.sweep * (key === graph.root
                    ? 1 / branch.children.length : next.weight / branch.weight);
                next.sweep = Math.min(allocation, Math.PI / 2);
                next.angle = start + allocation / 2;
                next.start = next.angle - next.sweep / 2;
                start += allocation;
            });
        });
        const gap = 24;
        const cellWidth = width + gap;
        const cellHeight = height + gap;
        const occupied = new Map([["0:0", {x: 0, y: 0}]]);
        order.slice(1).forEach(key => {
            const branch = branches.get(key);
            const node = nodes.get(key);
            const parent = nodes.get(branch.parent);
            const origin = {x: parent.x + width / 2, y: parent.y + height / 2};
            const dx = Math.cos(branch.angle);
            const dy = Math.sin(branch.angle);
            let distance = Math.min(cellWidth / Math.abs(dx), cellHeight / Math.abs(dy)) + 0.001;
            let center;
            let cellX;
            let cellY;
            let blocked;
            do {
                center = {x: origin.x + dx * distance, y: origin.y + dy * distance};
                cellX = Math.floor(center.x / cellWidth);
                cellY = Math.floor(center.y / cellHeight);
                blocked = false;
                // Query only neighboring cells, then jump past blockers along this flow.
                for (let x = cellX - 1; x <= cellX + 1; x++) {
                    for (let y = cellY - 1; y <= cellY + 1; y++) {
                        const other = occupied.get(`${x}:${y}`);
                        if (!other || Math.abs(center.x - other.x) >= cellWidth
                            || Math.abs(center.y - other.y) >= cellHeight) continue;
                        const exitX = dx ? (other.x + Math.sign(dx) * cellWidth - origin.x) / dx : Infinity;
                        const exitY = dy ? (other.y + Math.sign(dy) * cellHeight - origin.y) / dy : Infinity;
                        distance = Math.max(distance, Math.min(exitX, exitY) + 0.001);
                        blocked = true;
                    }
                }
            } while (blocked);
            occupied.set(`${cellX}:${cellY}`, center);
            node.x = center.x - width / 2;
            node.y = center.y - height / 2;
        });
        drawPositions();
        fit();
    }

    function detailLine(label, value) {
        if (value === undefined || value === null || value === "") return;
        const line = document.createElement("p");
        const heading = document.createElement("strong");
        heading.textContent = `${label}: `;
        line.append(heading, document.createTextNode(typeof value === "object" ? JSON.stringify(value) : String(value)));
        details.append(line);
    }

    function inspect(item, edge = false) {
        selected?.setAttribute("aria-pressed", "false");
        selected?.classList.remove("is-selected");
        selected = item.element;
        selected.classList.add("is-selected");
        selected.setAttribute("aria-pressed", "true");
        highlightFlow(item, edge);
        details.replaceChildren();
        detailLine(edge ? "Relationship" : labels[item.type], edge
            ? `${nodes.get(item.source).label} \u2192 ${nodes.get(item.target).label}` : item.label);
        if (edge) detailLine("Operation", item.label);
        detailLine("Status", item.status);
        detailLine("Source", item.path);
        detailLine("Physical table", item.physicalTable);
        detailLine("Scope", item.scope);
        detailLine("Parameters", item.parameters);
        detailLine("Invocation contexts", item.contexts);
        (item.evidence || []).forEach(source => {
            detailLine("Evidence", `${source.file}${source.line ? `:${source.line}` : ""}${source.jsonPointer || ""}${source.partition ? ` (partition: ${source.partition})` : ""}`);
        });
        if (item.status === "unverified") {
            detailLine("Caution", "Table names match, but schema/workspace/lakehouse identity is not proven. This is a candidate, not confirmed lineage.");
        }
        detailLine("Highlighted flow", `${pathNodes.size} nodes and ${pathEdges.size} relationships. Use Fit selected flow to follow this path.`);
    }

    function clearHighlight() {
        pathNodes.forEach(id => nodes.get(id).element.classList.remove("is-on-path"));
        pathEdges.forEach(id => edgesById.get(id).element.classList.remove("is-on-path"));
        pathNodes.clear();
        pathEdges.clear();
    }

    function highlightFlow(item, edge) {
        clearHighlight();
        function trace(start, adjacency, reverse) {
            const pending = [start];
            const visited = new Set();
            while (pending.length) {
                const key = pending.pop();
                if (visited.has(key)) continue;
                visited.add(key);
                pathNodes.add(key);
                adjacency.get(key).forEach(link => {
                    if (!link.visible) return;
                    pathEdges.add(link.id);
                    pending.push(reverse ? link.source : link.target);
                });
            }
        }
        // Trace each direction separately: ancestors must not pull in sibling flows.
        trace(edge ? item.source : item.id, incoming, true);
        trace(edge ? item.target : item.id, outgoing, false);
        if (edge) pathEdges.add(item.id);
        pathNodes.forEach(id => nodes.get(id).element.classList.add("is-on-path"));
        pathEdges.forEach(id => edgesById.get(id).element.classList.add("is-on-path"));
        svg.classList.add("has-focused-flow");
        fitFlow.disabled = false;
        clearFlow.disabled = false;
        updateStatus();
    }

    function clearSelection() {
        clearHighlight();
        selected?.classList.remove("is-selected");
        selected?.setAttribute("aria-pressed", "false");
        selected = null;
        svg.classList.remove("has-focused-flow");
        fitFlow.disabled = true;
        clearFlow.disabled = true;
        details.textContent = "Select a node or relationship to highlight its flow and inspect its evidence.";
        updateStatus();
    }

    edges.forEach(edge => {
        const group = element("g", {
            class: `lineage-graph-edge${edge.status === "unverified" ? " is-unverified" : ""}`,
            "data-edge-id": edge.id, tabindex: "0", role: "button", "aria-pressed": "false",
            "aria-label": `${nodes.get(edge.source).label} ${edge.label} ${nodes.get(edge.target).label}, ${edge.status}`,
        });
        const path = element("path", {"marker-end": "url(#lineageArrow)"});
        const caption = element("text", {class: "lineage-edge-caption", "text-anchor": "middle"},
            `${edge.label}${edge.status === "unverified" ? " (unverified)" : ""}`);
        group.append(element("title", {}, group.getAttribute("aria-label")), path, caption);
        edgeLayer.append(group);
        Object.assign(edge, {element: group, path, caption});
    });

    nodes.forEach(node => {
        const group = element("g", {
            class: "lineage-graph-node", "data-node-id": node.id, "data-type": node.type,
            tabindex: "0", role: "button", "aria-pressed": "false", "aria-label": `${labels[node.type]}: ${node.label}`,
        });
        const caption = node.physicalTable || node.path || node.status || "";
        const truncate = (value, limit) => value.length > limit ? `${value.slice(0, limit - 3)}...` : value;
        const card = element("g", {class: "lineage-node-card"});
        card.append(
            element("rect", {width, height, rx: "9"}),
            element("text", {x: "12", y: "18", class: "lineage-node-type"}, labels[node.type]),
            element("text", {x: "12", y: "40", class: "lineage-node-label"}, truncate(node.label, 28)),
            element("text", {x: "12", y: "61", class: "lineage-node-caption"}, truncate(caption, 33)),
        );
        group.append(
            element("title", {}, `${labels[node.type]}: ${node.label}\n${caption}`),
            element("circle", {cx: width / 2, cy: height / 2, r: 10, class: "lineage-node-dot"}),
            card,
        );
        node.element = group;
        nodeLayer.append(group);
    });
    svg.append(defs, viewport);

    const pointers = new Map();
    let gesture = null;
    let moved = false;
    let suppressClick = false;
    svg.addEventListener("pointerdown", event => {
        if (event.button !== 0) return;
        const position = point(event);
        pointers.set(event.pointerId, position);
        svg.setPointerCapture(event.pointerId);
        const node = nodes.get(event.target.closest("[data-node-id]")?.dataset.nodeId);
        const edge = edgesById.get(event.target.closest("[data-edge-id]")?.dataset.edgeId);
        gesture = {node, edge, position};
        moved = false;
        if (pointers.size === 2) gesture = null;
    });
    svg.addEventListener("pointermove", event => {
        if (!pointers.has(event.pointerId)) return;
        const previous = pointers.get(event.pointerId);
        const current = point(event);
        if (pointers.size === 2) {
            const other = [...pointers.entries()].find(([id]) => id !== event.pointerId)[1];
            const oldDistance = Math.hypot(previous.x - other.x, previous.y - other.y);
            const newDistance = Math.hypot(current.x - other.x, current.y - other.y);
            if (oldDistance > 0) zoom(scale * newDistance / oldDistance, {
                x: (current.x + other.x) / 2, y: (current.y + other.y) / 2,
            });
            moved = true;
        } else if (gesture) {
            const dx = current.x - previous.x;
            const dy = current.y - previous.y;
            if (Math.hypot(current.x - gesture.position.x, current.y - gesture.position.y) > 3) moved = true;
            if (gesture.node) {
                gesture.node.x += dx / scale;
                gesture.node.y += dy / scale;
                drawPositions(gesture.node);
            } else {
                offset.x += dx;
                offset.y += dy;
                transform();
            }
        }
        pointers.set(event.pointerId, current);
    });
    function endPointer(event) {
        if (!pointers.has(event.pointerId)) return;
        if (!moved && gesture?.node && event.type === "pointerup") inspect(gesture.node);
        if (!moved && gesture?.edge && event.type === "pointerup") inspect(gesture.edge, true);
        suppressClick = moved;
        pointers.delete(event.pointerId);
        gesture = null;
        if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    }
    svg.addEventListener("pointerup", endPointer);
    svg.addEventListener("pointercancel", endPointer);
    svg.addEventListener("click", event => {
        if (suppressClick) {
            event.stopPropagation();
            suppressClick = false;
        }
    }, true);
    svg.addEventListener("wheel", event => {
        event.preventDefault();
        zoom(scale * Math.exp(-event.deltaY * 0.0015), point(event));
    }, {passive: false});
    svg.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            clearSelection();
            event.preventDefault();
            return;
        }
        const node = nodes.get(event.target.closest("[data-node-id]")?.dataset.nodeId);
        const edge = edgesById.get(event.target.closest("[data-edge-id]")?.dataset.edgeId);
        if ((event.key === "Enter" || event.key === " ") && (node || edge)) {
            event.preventDefault();
            inspect(node || edge, Boolean(edge));
            return;
        }
        const nodeMoves = {ArrowLeft: [-15, 0], ArrowRight: [15, 0], ArrowUp: [0, -15], ArrowDown: [0, 15]};
        if (node && nodeMoves[event.key]) {
            event.preventDefault();
            node.x += nodeMoves[event.key][0];
            node.y += nodeMoves[event.key][1];
            drawPositions(node);
            return;
        }
        const moves = {ArrowLeft: [30, 0], ArrowRight: [-30, 0], ArrowUp: [0, 30], ArrowDown: [0, -30]};
        if (moves[event.key]) {
            offset.x += moves[event.key][0];
            offset.y += moves[event.key][1];
            transform();
        } else if (event.key === "+" || event.key === "=") zoom(scale * 1.2);
        else if (event.key === "-") zoom(scale / 1.2);
        else if (event.key === "0") fit();
        else return;
        event.preventDefault();
    });
    document.getElementById("lineageZoomIn").addEventListener("click", () => zoom(scale * 1.2));
    document.getElementById("lineageZoomOut").addEventListener("click", () => zoom(scale / 1.2));
    document.getElementById("lineageFit").addEventListener("click", () => fit());
    fitFlow.addEventListener("click", () => fit(true));
    clearFlow.addEventListener("click", clearSelection);
    document.getElementById("lineageReset").addEventListener("click", layout);
    search.addEventListener("input", () => {
        const query = search.value.trim().toLocaleLowerCase();
        let matches = 0;
        nodes.forEach(node => {
            const match = Boolean(query) && node.visible
                && `${node.label} ${node.physicalTable || ""} ${node.path || ""}`.toLocaleLowerCase().includes(query);
            node.element.classList.toggle("is-search-match", match);
            if (match) matches++;
        });
        if (query) status.textContent = `${matches} matching nodes. Press Enter to focus the first match.`;
        else updateStatus();
    });
    search.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        const match = [...nodes.values()].find(node => node.element.classList.contains("is-search-match"));
        if (match) {
            scale = 1;
            offset = {x: svg.clientWidth / 2 - match.x - width / 2, y: svg.clientHeight / 2 - match.y - height / 2};
            transform();
            match.element.focus({preventScroll: true});
            inspect(match);
        }
    });
    [...nodes.values()].filter(node => node.type === "Table").forEach(node => {
        const option = document.createElement("option");
        option.value = node.id;
        option.textContent = `${node.label}${node.physicalTable ? ` (${node.physicalTable})` : ""}`;
        tableFilter.append(option);
    });
    function updateStatus() {
        const visibleNodes = [...nodes.values()].filter(node => node.visible).length;
        const visibleEdges = edges.filter(edge => edge.visible);
        status.textContent = `${visibleNodes} of ${nodes.size} nodes and ${visibleEdges.length} relationships shown. `
            + `${visibleEdges.filter(edge => edge.status === "unverified").length} unverified relationships. `
            + (includeReads.checked ? "Notebook writes and reads included." : "Notebook writes only.")
            + (pathNodes.size ? ` Selected flow: ${pathNodes.size} nodes and ${pathEdges.size} relationships.` : "");
    }

    function updateVisibility() {
        const enabled = edge => includeReads.checked || edge.label !== "read by";
        const visible = new Set(tableFilter.value ? [graph.root] : []);
        const pending = [tableFilter.value || graph.root];
        while (pending.length) {
            const key = pending.pop();
            if (visible.has(key)) continue;
            visible.add(key);
            outgoing.get(key).forEach(edge => {
                if (enabled(edge)) pending.push(edge.target);
            });
        }
        nodes.forEach(node => {
            node.visible = visible.has(node.id);
            node.element.classList.toggle("is-hidden", !node.visible);
            node.element.classList.remove("is-selected");
            node.element.classList.remove("is-search-match");
        });
        edges.forEach(edge => {
            edge.visible = enabled(edge) && visible.has(edge.source) && visible.has(edge.target);
            edge.element.classList.toggle("is-hidden", !edge.visible);
            edge.element.classList.remove("is-selected");
        });
        clearSelection();
        search.value = "";
        updateStatus();
        layout();
    }
    tableFilter.addEventListener("change", updateVisibility);
    includeReads.addEventListener("change", updateVisibility);
    updateVisibility();
    new ResizeObserver(() => fit()).observe(svg);
})();
