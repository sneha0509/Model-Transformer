const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
    constructor(tag = "div") {
        this.tag = tag;
        this.nodes = [];
        this.dataset = {};
        this.listeners = new Map();
        this.className = "";
        this.value = "";
        this.hidden = false;
        this.classList = {
            contains: name => this.className.split(/\s+/).includes(name),
            add: name => this.classList.toggle(name, true),
            remove: name => this.classList.toggle(name, false),
            toggle: (name, enabled = !this.classList.contains(name)) => {
                const names = new Set(this.className.split(/\s+/).filter(Boolean));
                if (enabled) names.add(name);
                else names.delete(name);
                this.className = [...names].join(" ");
            },
        };
    }

    get children() { return this.nodes.filter(node => node instanceof Element); }
    get textContent() { return this.nodes.map(node => node.textContent).join(""); }
    set textContent(value) { this.nodes = [{textContent: String(value)}]; }
    append(...nodes) { this.nodes.push(...nodes); }
    replaceChildren(...nodes) { this.nodes = [...nodes]; }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    reportValidity() { return true; }
    focus() {}

    querySelectorAll(selector) {
        const matches = node => selector.split(",").some(part => {
            const value = part.trim();
            if (value.startsWith(".")) return node.classList.contains(value.slice(1));
            if (value === "[data-table-toggle]") return node.dataset.tableToggle !== undefined;
            return node.tag === value;
        });
        return this.children.flatMap(child => [
            ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
        ]);
    }
}

function reportFixture() {
    const finding = name => ({
        artifactKey: "Test.SemanticModel", objectType: "measure", objectName: name,
        changeIds: ["change-1"], severity: "high", confidence: "high",
        impact: ["The revenue calculation fails after removal of Orders[Amount]."],
        recommendation: ["Specify replacement revenue logic or approve measure retirement."],
        proposedFixes: [], fixUnavailableReason: "A replacement revenue calculation has not been specified.",
        evidence: [{file: "model.bim", line: 1, excerpt: "SUM(Orders[Amount])"}],
    });
    return {
        instructions: "Remove Orders[Amount].",
        changes: [{id: "change-1", table: "Orders", column: "Amount"}],
        inventory: [{key: "Test.SemanticModel", name: "Test", type: "SemanticModel"}],
        targets: [], impacts: [finding("Orders.Revenue"), finding("Orders.Revenue Twice")],
        unaffectedTargets: [], gaps: [], impactLineage: {nodes: [], edges: []},
    };
}

async function renderReport(report) {
    const nodes = new Map();
    const document = {
        getElementById: id => {
            if (!nodes.has(id)) nodes.set(id, new Element());
            return nodes.get(id);
        },
        createElement: tag => new Element(tag),
        createTextNode: text => ({textContent: text}),
    };
    const node = id => document.getElementById(id);
    node("fabricImpact").dataset = {catalogUrl: "catalog", analyzeUrl: "analyze"};
    node("impactInstructions").value = report.instructions;
    const catalog = {
        revision: "test", tables: [{name: "Orders", columns: [{name: "Amount", dataType: "double"}]}],
        issues: [], semanticModel: {name: "Test"},
    };
    const window = {addEventListener() {}, print() {}};
    const fetch = async url => {
        if (url === "catalog") return {ok: true, json: async () => catalog};
        assert.equal(url, "analyze");
        let consumed = false;
        const content = new TextEncoder().encode(`data: ${JSON.stringify({state: "complete", report})}\n\n`);
        return {
            ok: true, body: {getReader: () => ({
                read: async () => {
                    if (consumed) return {done: true};
                    consumed = true;
                    return {done: false, value: content};
                },
                cancel: async () => {}, releaseLock() {},
            })},
        };
    };
    const script = fs.readFileSync(path.join(__dirname, "..", "frontend", "static", "js", "impact.js"), "utf8");
    vm.runInNewContext(script, {document, window, fetch, TextDecoder, Uint8Array});
    await new Promise(resolve => setImmediate(resolve));
    assert.match(node("impactStatus").textContent, /tables available/);
    const checkbox = node("impactTables").querySelectorAll("input").find(input => !input.dataset.tableToggle);
    checkbox.checked = true;
    await node("impactForm").listeners.get("submit")({preventDefault() {}});
    assert.equal(node("impactStatus").textContent, "Analysis complete. Your report is ready.");
    return node;
}

test("confirmed DAX failures remain visible without automatic fixes", async () => {
    const node = await renderReport(reportFixture());
    const findings = node("impactFindings").textContent;
    assert.match(findings, /Orders\.Revenue/);
    assert.match(findings, /Orders\.Revenue Twice/);
    assert.match(findings, /fails after removal/);
    assert.match(findings, /Specify replacement revenue logic/);
    assert.match(findings, /No automatic fix available/);
    assert.match(findings, /replacement revenue calculation has not been specified/);
    assert.match(node("impactReportSummary").textContent, /2 impact findings/);
    assert.match(node("impactReportSummary").textContent, /0 with generated code; 2 without automatic fixes/);
    assert.match(node("impactReportMetrics").textContent, /Impact findings2/);
    assert.equal(node("impactReviewNotes").hidden, true);
    assert.match(node("impactArtifactFilters").textContent, /Semantic models \(1\)/);
});

test("mixed findings preserve generated code and impacts awaiting decisions", async () => {
    const report = reportFixture();
    report.impacts[0].proposedFixes = [{
        file: "model.bim", before: "SUM(Orders[Amount])", after: "SUM(Orders[Revenue])",
        diff: "-SUM(Orders[Amount])\n+SUM(Orders[Revenue])",
    }];
    report.impacts[0].fixUnavailableReason = null;
    const node = await renderReport(report);
    assert.match(node("impactFindings").textContent, /After \(AI proposed\)/);
    assert.match(node("impactFindings").textContent, /Unified diff/);
    assert.match(node("impactFindings").textContent, /No automatic fix available/);
    assert.match(node("impactReportSummary").textContent, /1 with generated code; 1 without automatic fixes/);
});

test("unresolved analysis stays a gap rather than a false impact", async () => {
    const report = reportFixture();
    report.impacts = [];
    report.gaps = [{file: "Test.SemanticModel", message: "The requested change is ambiguous."}];
    const node = await renderReport(report);
    assert.match(node("impactReportSummary").textContent, /analysis is incomplete/);
    assert.match(node("impactReportGaps").textContent, /requested change is ambiguous/);
    assert.equal(node("impactArtifactFilters").hidden, true);
    assert.equal(node("impactReviewNotes").hidden, false);
});

test("older impact findings are not hidden solely because they lack code", async () => {
    const report = reportFixture();
    report.impacts.forEach(finding => {
        delete finding.proposedFixes;
        delete finding.fixUnavailableReason;
        finding.impact = "The measure references the removed column.";
    });
    const node = await renderReport(report);
    assert.match(node("impactFindings").textContent, /references the removed column/);
    assert.match(node("impactFindings").textContent, /Rerun analysis to assess fix availability/);
    assert.equal(node("impactReviewNotes").hidden, true);
});
