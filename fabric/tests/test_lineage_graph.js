const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

class Element {
    constructor(name) {
        this.name = name;
        this.children = [];
        this.attributes = {};
        this.listeners = new Map();
        this.classes = new Set();
        this.value = "";
        this.checked = false;
        this.disabled = false;
        this.clientWidth = 1400;
        this.clientHeight = 680;
        this.classList = {
            add: (...names) => names.forEach(name => this.classes.add(name)),
            remove: (...names) => names.forEach(name => this.classes.delete(name)),
            contains: name => this.classes.has(name),
            toggle: (name, enabled = !this.classes.has(name)) => {
                if (enabled) this.classes.add(name);
                else this.classes.delete(name);
            },
        };
    }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "class") this.classes = new Set(value.split(" "));
    }
    getAttribute(name) { return this.attributes[name]; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    addEventListener(name, listener) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(listener);
    }
    dispatch(name, options = {}) {
        const event = {target: this, preventDefault() {}, ...options};
        (this.listeners.get(name) || []).forEach(listener => listener(event));
    }
    closest() { return null; }
    focus() {}
}

function graphFixture({shared = false, cycle = false, count = 3} = {}) {
    const nodes = [{id: "model", type: "SemanticModel", label: "Model"}];
    const edges = [];
    const link = (source, target, label) => edges.push({
        id: `${source}->${target}:${label}`, source, target, label, status: "confirmed",
    });
    for (let index = 0; index < count; index++) {
        nodes.push(
            {id: `table:${index}`, type: "Table", label: `Table ${index}`},
            {id: `notebook:${index}`, type: "Notebook", label: `Notebook ${index}`},
        );
        const pipeline = shared ? "pipeline:0" : `pipeline:${index}`;
        if (!nodes.some(node => node.id === pipeline)) nodes.push({id: pipeline, type: "DataPipeline", label: pipeline});
        link("model", `table:${index}`, "uses");
        link(`table:${index}`, `notebook:${index}`, "written by");
        link(`notebook:${index}`, pipeline, "run by");
    }
    nodes.push({id: "reader", type: "Notebook", label: "Reader"});
    link("table:0", "reader", "read by");
    if (cycle) {
        nodes.push({id: "caller", type: "Notebook", label: "Caller"});
        link("notebook:0", "caller", "called by");
        link("caller", "notebook:0", "called by");
    }
    return {root: "model", nodes, edges};
}

function viewer(graph) {
    const elements = new Map();
    const frames = [];
    const get = id => {
        if (!elements.has(id)) elements.set(id, new Element(id));
        return elements.get(id);
    };
    get("lineageGraphData").textContent = JSON.stringify(graph);
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, "..", "frontend", "static", "js", "lineage-graph.js"), "utf8"),
        {
            document: {
                getElementById: get,
                createElement: name => new Element(name),
                createElementNS: (_namespace, name) => new Element(name),
                createTextNode: text => ({textContent: text}),
            },
            requestAnimationFrame: callback => frames.push(callback),
            ResizeObserver: class { observe() {} },
        },
    );
    const flush = () => {
        while (frames.length) frames.shift()();
    };
    flush();
    const svg = get("lineageGraph");
    const viewport = svg.children[1];
    const nodes = new Map(viewport.children[1].children.map(node => [node.getAttribute("data-node-id"), node]));
    const edges = new Map(viewport.children[0].children.map(edge => [edge.getAttribute("data-edge-id"), edge]));
    function select(id, edge = false) {
        svg.dispatch("keydown", {key: "Enter", target: {
            closest: selector => selector === (edge ? "[data-edge-id]" : "[data-node-id]")
                ? {dataset: edge ? {edgeId: id} : {nodeId: id}} : null,
        }});
        flush();
    }
    const change = (id, value) => {
        const control = get(id);
        if (typeof value === "boolean") control.checked = value;
        else control.value = value;
        control.dispatch("change");
        flush();
    };
    return {get, nodes, edges, select, change, flush, svg, viewport};
}

function position(node) {
    const [, x, y] = /translate\(([-\d.e+]+) ([-\d.e+]+)\)/.exec(node.getAttribute("transform"));
    return {x: Number(x) + 118, y: Number(y) + 38};
}

const onPath = elements => [...elements].filter(([, element]) => element.classList.contains("is-on-path")).map(([id]) => id).sort();

function visiblePositions(graph) {
    return [...graph.nodes.values()].filter(node => !node.classList.contains("is-hidden")).map(position);
}

function bounds(positions) {
    return {
        width: Math.max(...positions.map(value => value.x)) - Math.min(...positions.map(value => value.x)) + 236,
        height: Math.max(...positions.map(value => value.y)) - Math.min(...positions.map(value => value.y)) + 76,
    };
}

function assertClearance(positions) {
    for (let index = 0; index < positions.length; index++) {
        for (let other = index + 1; other < positions.length; other++) {
            assert.ok(Math.abs(positions[index].x - positions[other].x) >= 259.99
                || Math.abs(positions[index].y - positions[other].y) >= 99.99);
        }
    }
}

test("connected flows occupy their own branches, with the model centered", () => {
    const graph = viewer(graphFixture());
    assert.deepEqual(position(graph.nodes.get("model")), {x: 0, y: 0});
    for (let index = 0; index < 3; index++) {
        const table = position(graph.nodes.get(`table:${index}`));
        const notebook = position(graph.nodes.get(`notebook:${index}`));
        const pipeline = position(graph.nodes.get(`pipeline:${index}`));
        assert.ok(Math.abs(Math.atan2(table.y, table.x) - Math.atan2(notebook.y, notebook.x)) < 1e-10);
        assert.ok(Math.abs(Math.atan2(table.y, table.x) - Math.atan2(pipeline.y, pipeline.x)) < 1e-10);
        assert.ok(Math.hypot(table.x, table.y) < Math.hypot(notebook.x, notebook.y));
        assert.ok(Math.hypot(notebook.x, notebook.y) < Math.hypot(pipeline.x, pipeline.y));
    }
});

test("selecting a table highlights its full flow without pulling in siblings", () => {
    const graph = viewer(graphFixture());
    graph.select("table:0");
    assert.deepEqual(onPath(graph.nodes), ["model", "notebook:0", "pipeline:0", "table:0"]);
    assert.equal(onPath(graph.edges).length, 3);
    assert.equal(graph.get("lineageFitFlow").disabled, false);
    assert.equal(graph.get("lineageClearFlow").disabled, false);
    assert.equal(graph.svg.classList.contains("has-focused-flow"), true);
    graph.get("lineageFitFlow").dispatch("click");
    graph.flush();
    assert.ok(graph.viewport.getAttribute("transform").includes("scale("));
    graph.get("lineageClearFlow").dispatch("click");
    assert.deepEqual(onPath(graph.nodes), []);
    assert.equal(graph.svg.classList.contains("has-focused-flow"), false);
    assert.equal(graph.nodes.get("table:0").getAttribute("aria-pressed"), "false");
});

test("shared pipelines remain single and only contribute paths through the selected item", () => {
    const graph = viewer(graphFixture({shared: true}));
    assert.equal([...graph.nodes.keys()].filter(id => id.startsWith("pipeline:")).length, 1);
    graph.select("table:0");
    assert.deepEqual(onPath(graph.nodes), ["model", "notebook:0", "pipeline:0", "table:0"]);
    graph.select("pipeline:0");
    assert.deepEqual(onPath(graph.nodes), [
        "model", "notebook:0", "notebook:1", "notebook:2", "pipeline:0", "table:0", "table:1", "table:2",
    ]);
    graph.select("notebook:1->pipeline:0:run by", true);
    assert.deepEqual(onPath(graph.nodes), ["model", "notebook:1", "pipeline:0", "table:1"]);
});

test("optional reads and table filters reset the path and never expose hidden relationships", () => {
    const graph = viewer(graphFixture());
    graph.select("table:0");
    assert.equal(graph.nodes.get("reader").classList.contains("is-hidden"), true);
    graph.change("lineageGraphReads", true);
    assert.deepEqual(onPath(graph.nodes), []);
    graph.select("table:0");
    assert.equal(onPath(graph.nodes).includes("reader"), true);
    graph.change("lineageGraphTable", "table:1");
    graph.select("pipeline:1");
    assert.deepEqual(onPath(graph.nodes), ["model", "notebook:1", "pipeline:1", "table:1"]);
    graph.change("lineageGraphReads", false);
    assert.equal(graph.get("lineageFitFlow").disabled, true);
});

test("cycles terminate, and notebook callers follow their connections rather than a type ring", () => {
    const graph = viewer(graphFixture({cycle: true}));
    graph.select("notebook:0");
    assert.deepEqual(onPath(graph.nodes), ["caller", "model", "notebook:0", "pipeline:0", "table:0"]);
    const caller = position(graph.nodes.get("caller"));
    const notebook = position(graph.nodes.get("notebook:0"));
    assert.ok(Math.hypot(caller.x, caller.y) > Math.hypot(notebook.x, notebook.y));
    graph.svg.dispatch("keydown", {key: "Escape"});
    assert.deepEqual(onPath(graph.nodes), []);
});

test("isolating a branching table keeps its callers and pipeline in the same outward flow", () => {
    const graph = viewer(graphFixture({cycle: true}));
    graph.change("lineageGraphTable", "table:0");
    const notebook = position(graph.nodes.get("notebook:0"));
    for (const id of ["caller", "pipeline:0"]) {
        const next = position(graph.nodes.get(id));
        assert.ok(next.x > notebook.x);
        assert.ok(Math.abs(Math.atan2(next.y, next.x)) < Math.PI / 4);
    }
    graph.select("table:0");
    graph.get("lineageFitFlow").dispatch("click");
    graph.flush();
    assert.equal(graph.svg.classList.contains("is-compact"), false);
});

test("large branch layouts have finite, non-overlapping positions and retain single nodes", () => {
    const graph = viewer(graphFixture({shared: true, count: 1000}));
    const positions = visiblePositions(graph);
    assert.equal(positions.length, 2002);
    positions.forEach(value => assert.ok(Number.isFinite(value.x) && Number.isFinite(value.y)));
    assert.ok(Number.parseFloat(graph.get("lineageZoomLevel").textContent) > 0);
    assertClearance(positions);
    graph.select("table:500");
    assert.deepEqual(onPath(graph.nodes), ["model", "notebook:500", "pipeline:0", "table:500"]);
});

test("simple flows leave only a small card-to-card gap instead of a fixed large radial step", () => {
    const fixture = graphFixture();
    const graph = viewer(fixture);
    for (const edge of fixture.edges.filter(edge => edge.label !== "read by")) {
        const source = position(graph.nodes.get(edge.source));
        const target = position(graph.nodes.get(edge.target));
        const gap = Math.hypot(Math.max(0, Math.abs(target.x - source.x) - 236),
            Math.max(0, Math.abs(target.y - source.y) - 76));
        assert.ok(gap >= 23.99 && gap <= 25, `Unexpected gap: ${gap}`);
    }
    assertClearance(visiblePositions(graph));
});

test("relationship captions clear the cards when horizontal connectors are short", () => {
    const graph = viewer(graphFixture({count: 1}));
    const caption = graph.edges.get("table:0->notebook:0:written by").children[2];
    const table = position(graph.nodes.get("table:0"));
    const notebook = position(graph.nodes.get("notebook:0"));
    assert.ok(Number(caption.getAttribute("y")) < Math.min(table.y, notebook.y) - 38);
});

test("relationship captions fit inside short vertical gaps", () => {
    const graph = viewer(graphFixture({count: 4}));
    graph.select("table:0");
    graph.get("lineageFitFlow").dispatch("click");
    graph.flush();
    const caption = graph.edges.get("table:0->notebook:0:written by").children[2];
    const table = position(graph.nodes.get("table:0"));
    const notebook = position(graph.nodes.get("notebook:0"));
    const baseline = Number(caption.getAttribute("y"));
    assert.ok(baseline - 11 >= Math.min(table.y, notebook.y) + 38);
    assert.ok(baseline + 3 <= Math.max(table.y, notebook.y) - 38);
});

test("many table flows occupy a compact footprint without overlaps", () => {
    const graph = viewer(graphFixture({count: 24}));
    const positions = visiblePositions(graph);
    const size = bounds(positions);
    assert.ok(size.width < 3000, `Graph width: ${size.width}`);
    assert.ok(size.height < 3000, `Graph height: ${size.height}`);
    assertClearance(positions);
});

test("crowding one branch does not stretch an unrelated simple flow", () => {
    const fixture = graphFixture();
    for (let index = 0; index < 40; index++) {
        fixture.nodes.push({id: `extra:${index}`, type: "Notebook", label: `Extra ${index}`});
        fixture.edges.push({
            id: `extra-edge:${index}`, source: "table:0", target: `extra:${index}`,
            label: "written by", status: "confirmed",
        });
    }
    const graph = viewer(fixture);
    const table = position(graph.nodes.get("table:2"));
    const notebook = position(graph.nodes.get("notebook:2"));
    const hop = Math.hypot(notebook.x - table.x, notebook.y - table.y);
    assert.ok(hop < 300, `Unrelated flow hop: ${hop}`);
    assertClearance(visiblePositions(graph));
    graph.change("lineageGraphReads", true);
    assertClearance(visiblePositions(graph));
    graph.change("lineageGraphTable", "table:2");
    assertClearance(visiblePositions(graph));
});

test("a model with no source tables still fits and can be selected and cleared", () => {
    const graph = viewer({root: "model", nodes: [
        {id: "model", type: "SemanticModel", label: "Empty model"},
    ], edges: []});
    graph.select("model");
    assert.deepEqual(onPath(graph.nodes), ["model"]);
    assert.deepEqual(onPath(graph.edges), []);
    graph.get("lineageFitFlow").dispatch("click");
    graph.flush();
    assert.ok(graph.viewport.getAttribute("transform").includes("scale(1.15)"));
    graph.svg.dispatch("keydown", {key: "Escape"});
    assert.equal(graph.get("lineageFitFlow").disabled, true);
});
