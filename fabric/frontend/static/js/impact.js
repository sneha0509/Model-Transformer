(() => {
    const root = document.getElementById("fabricImpact");
    if (!root) return;
    const form = document.getElementById("impactForm");
    const tables = document.getElementById("impactTables");
    const status = document.getElementById("impactStatus");
    const analyze = document.getElementById("analyzeImpactButton");
    const reload = document.getElementById("reloadImpactSchema");
    const search = document.getElementById("impactSearch");
    const instructions = document.getElementById("impactInstructions");
    const results = document.getElementById("impactResults");
    const modelForm = document.getElementById("impactLiveModelForm");
    const modelSelect = document.getElementById("impactLiveModel");
    const loadModel = document.getElementById("loadImpactLiveModel");
    let catalog = null;
    let rows = [];
    let busy = false;
    const typeLabels = {
        Notebook: "Notebook", SemanticModel: "Semantic model", DataPipeline: "Pipeline",
        notebook_code: "Notebook cell", column: "Column", calculated_column: "Calculated column",
        measure: "Measure", table: "Table", calculated_table: "Calculated table",
        relationship: "Relationship", partition: "Source binding", hierarchy: "Hierarchy",
        role: "Security role", expression: "Source expression", pipeline_activity: "Pipeline activity",
        pipeline_parameter: "Pipeline parameter", pipeline_variable: "Pipeline variable",
    };

    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function message(text, error = false) {
        status.textContent = text;
        status.classList.toggle("is-error", error);
    }

    function updateSelection() {
        const selected = rows.filter(row => row.checkbox.checked);
        document.getElementById("impactSelectionCount").textContent =
            `${selected.length} ${selected.length === 1 ? "column" : "columns"} selected (maximum 200)`;
        tables.querySelectorAll("[data-table-toggle]").forEach(toggle => {
            const children = rows.filter(row => row.table === toggle.dataset.tableToggle);
            toggle.disabled = busy || !children.length;
            toggle.checked = children.length > 0 && children.every(row => row.checkbox.checked);
            toggle.indeterminate = children.some(row => row.checkbox.checked) && !toggle.checked;
        });
        analyze.disabled = busy || selected.length === 0 || selected.length > 200 || !instructions.value.trim();
    }

    function setBusy(value) {
        busy = value;
        form.setAttribute("aria-busy", String(value));
        form.querySelectorAll("input, select, textarea").forEach(control => {
            control.disabled = value;
        });
        reload.disabled = value;
        modelSelect.disabled = value;
        loadModel.disabled = value || !modelSelect.value;
        updateSelection();
    }

    function labeledControl(labelText, control) {
        const label = element("label", labelText);
        label.append(control);
        return label;
    }

    function renderCatalog() {
        rows = [];
        tables.replaceChildren();
        catalog.tables.forEach(table => {
            const group = element("details", undefined, "impact-table");
            const heading = element("summary");
            const toggle = element("input");
            toggle.type = "checkbox";
            toggle.dataset.tableToggle = table.name;
            toggle.disabled = !table.columns.length;
            toggle.setAttribute("aria-label", `Select all columns in ${table.name}`);
            toggle.addEventListener("click", event => event.stopPropagation());
            heading.append(toggle, document.createTextNode(` ${table.name} (${table.columns.length} columns)`));
            group.append(heading, element("p",
                `Physical sources: ${table.sources.map(source => source.table).join(", ") || "Unresolved or calculated"}; source status: ${table.sourceStatus}`,
                "fabric-status"));
            table.columns.forEach(column => {
                const container = element("div", undefined, "impact-column");
                const checkbox = element("input");
                checkbox.type = "checkbox";
                const label = labeledControl(` ${column.name} `, checkbox);
                container.append(label,
                    element("small", `${column.dataType || "unknown type"} | source: ${column.sourceColumn || "(calculated / unmapped)"}`));
                const row = {table: table.name, column: column.name, checkbox, container, group};
                rows.push(row);
                checkbox.addEventListener("change", updateSelection);
                group.append(container);
            });
            toggle.addEventListener("change", () => {
                rows.filter(row => row.table === table.name).forEach(row => {
                    row.checkbox.checked = toggle.checked;
                });
                updateSelection();
            });
            tables.append(group);
        });
        updateSelection();
        filterTables();
    }

    function filterTables() {
        const query = search.value.trim().toLocaleLowerCase();
        rows.forEach(row => {
            row.container.hidden = !`${row.table} ${row.column}`.toLocaleLowerCase().includes(query);
        });
        tables.querySelectorAll(".impact-table").forEach(group => {
            group.hidden = rows.some(row => row.group === group) &&
                rows.filter(row => row.group === group).every(row => row.container.hidden);
            if (query && !group.hidden) group.open = true;
        });
    }

    async function responseError(response) {
        if (response.headers.get("content-type")?.includes("application/json")) {
            const payload = await response.json();
            if (payload.requiresModelSelection) showModelChoices(payload.semanticModels);
            return payload.message || `Request failed (${response.status}).`;
        }
        return `Request failed (${response.status}). Check the application log.`;
    }

    function showModelChoices(models) {
        catalog = null;
        rows = [];
        tables.replaceChildren();
        results.hidden = true;
        modelSelect.replaceChildren();
        const placeholder = element("option", "Select the matching live model");
        placeholder.value = "";
        modelSelect.append(placeholder);
        models.forEach(model => {
            const option = element("option", `${model.name} (${model.id})`);
            option.value = model.id;
            modelSelect.append(option);
        });
        modelForm.hidden = models.length === 0;
        loadModel.disabled = true;
        updateSelection();
    }

    async function loadCatalog(modelId) {
        setBusy(true);
        catalog = null;
        rows = [];
        tables.replaceChildren();
        results.hidden = true;
        message("Fetching this semantic model's current tables and columns from the Fabric API.");
        try {
            const response = await fetch(root.dataset.catalogUrl, modelId ? {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({modelId}),
            } : undefined);
            if (!response.ok) throw new Error(await responseError(response));
            catalog = await response.json();
            modelForm.hidden = true;
            renderCatalog();
            const list = document.getElementById("impactCatalogGapList");
            list.replaceChildren(...catalog.issues.map(issue => element("li", `${issue.file}: ${issue.message}`)));
            document.getElementById("impactCatalogGaps").hidden = !catalog.issues.length;
            message(`${catalog.tables.length} tables loaded from Fabric for ${catalog.semanticModel.name}. Select impacted columns and describe the changes.`);
        } catch (error) {
            message(error.message, true);
        } finally {
            setBusy(false);
        }
    }

    function showReport(report) {
        results.hidden = false;
        const analyzed = report.inventory.filter(item => item.status === "analyzed").length;
        const scoped = report.inventory.filter(item => item.status !== "out_of_scope").length;
        const usage = report.aiUsage || {requests: 0, cacheHits: 0};
        document.getElementById("impactReportSummary").textContent =
            `${(report.targets || []).length} code-identified targets in ${scoped} artifacts. ` +
            `${analyzed} artifacts processed. AI calls: ${usage.requests}; reused: ${usage.cacheHits}. ` +
            `Coverage: ${report.coverage}. No changes applied.`;
        document.getElementById("impactReportDownload").href =
            root.dataset.reportBase.replace("REPORT_ID", report.reportId);
        document.getElementById("impactSummaries").replaceChildren(
            element("h3", "Your analysis instructions"),
            element("p", report.instructions, "impact-saved-instructions"),
            ...report.summaries.map(summary => element("p", summary)));
        const targets = document.getElementById("impactTargets");
        targets.replaceChildren();
        report.inventory.filter(item => item.targetCount).forEach(artifact => {
            const group = element("details", undefined, "impact-finding");
            group.append(element("summary",
                `${artifact.name} - ${typeLabels[artifact.type] || artifact.type} - targets: ${artifact.targetCount}`));
            const list = element("ul");
            (report.targets || []).filter(target => target.artifactKey === artifact.key).forEach(target => {
                const row = element("li");
                row.append(element("strong", `${typeLabels[target.objectType] || target.objectType}: ${target.objectName}`),
                    element("p", `${target.reason} (${target.confidence.replaceAll("_", " ")})`, "fabric-status"));
                list.append(row);
            });
            group.append(list);
            targets.append(group);
        });
        const lineage = report.impactLineage || {nodes: [], edges: []};
        const names = new Map(lineage.nodes.map(node => [node.id,
            node.objectType === "artifact" || !node.artifactKey ? node.objectName :
                `${node.artifactKey} / ${node.objectName}`]));
        const connections = element("table", undefined, "impact-lineage-table");
        const header = element("tr");
        ["From", "Dependency", "To", "Evidence level"].forEach(text => header.append(element("th", text)));
        const head = element("thead");
        head.append(header);
        const body = element("tbody");
        lineage.edges.filter(edge => edge.kind !== "contains").forEach(edge => {
            const row = element("tr");
            [names.get(edge.source), edge.kind.replaceAll("_", " "), names.get(edge.target),
                edge.confidence.replaceAll("_", " ")].forEach(text => row.append(element("td", text)));
            body.append(row);
        });
        connections.append(head, body);
        document.getElementById("impactLineage").replaceChildren(body.children.length ? connections :
            element("p", "No cross-object connection could be resolved. Review the target evidence and coverage gaps."));
        const findings = document.getElementById("impactFindings");
        findings.replaceChildren();
        if (!report.impacts.length) findings.append(element("p", "No evidenced impact was identified. Review coverage gaps before concluding the change is safe."));
        report.impacts.forEach(impact => {
            const card = element("details", undefined, "impact-finding");
            card.append(element("summary",
                `${impact.severity.toUpperCase()} - ${impact.artifactKey} - ${typeLabels[impact.objectType] || impact.objectType}: ${impact.objectName} (${impact.confidence} confidence)`));
            const changeNames = impact.changeIds.map(id => {
                const change = report.changes.find(item => item.id === id);
                return `${change.table}.${change.column}`;
            });
            card.append(element("p", `Changes: ${changeNames.join(", ")}`),
                element("h4", "What is the impact?"),
                element("p", impact.impact), element("h4", "What changes are required?"),
                element("p", impact.recommendation), element("h4", "Source evidence"));
            impact.evidence.forEach(citation => {
                const location = citation.cell === undefined ? `line ${citation.line}` :
                    `cell ${citation.cell}, line ${citation.cellLine} (export line ${citation.line})`;
                card.append(element("p", `${citation.file}: ${location}`),
                    element("pre", citation.excerpt));
            });
            card.append(element("h4", "Proposed changes (not applied)"));
            if (!impact.proposedFixes.length) card.append(element("p", "No automatic replacement proposed. Follow the recommendation above."));
            impact.proposedFixes.forEach(fix => {
                const cells = fix.cells ? ` (cells ${fix.cells.join(", ")})` : "";
                card.append(element("p", `${fix.file}${cells}: ${fix.rationale}`),
                    element("strong", "Before"), element("pre", fix.before),
                    element("strong", "After"), element("pre", fix.after));
            });
            card.append(element("h4", "Validation steps"));
            const checks = element("ol");
            checks.append(...impact.validationSteps.map(step => element("li", step)));
            card.append(checks);
            findings.append(card);
        });
        document.getElementById("impactReportGaps").replaceChildren(
            ...report.gaps.map(gap => element("li", `${gap.file ? gap.file + ": " : ""}${gap.message}`)),
            ...report.limitations.map(limitation => element("li", limitation)));
        document.getElementById("impactInventory").replaceChildren(
            ...report.inventory.map(item => element("li",
                `${item.type}: ${item.key} - ${item.status} (${item.origin === "fabric_api" ? "Fabric API" : "repository snapshot"})` +
                (item.selectedCells ? `; selected cells: ${item.selectedCells.join(", ")} of ${item.totalCodeCells} code cells` : "") +
                (item.contextBudget ? `; conservative input tokens: ${item.contextBudget.inputTokenUpperBound} / ` +
                    `${item.contextBudget.inputTokenLimit} (${item.contextBudget.model})` : ""))));
    }

    async function readAnalysis(response) {
        if (!response.body) throw new Error("The server did not provide analysis progress.");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let complete = false;
        try {
            while (true) {
                const {done, value} = await reader.read();
                buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
                const blocks = buffer.split("\n\n");
                buffer = blocks.pop();
                for (const block of blocks) {
                    const data = block.split("\n").find(line => line.startsWith("data: "));
                    if (!data) continue;
                    const event = JSON.parse(data.slice(6));
                    if (event.state === "error") throw new Error(event.message);
                    if (event.state === "complete") {
                        showReport(event.report);
                        complete = true;
                        message("Analysis complete. The review-only report is saved and ready to download.");
                    } else message(event.message);
                }
                if (done) break;
            }
            if (!complete) throw new Error("Analysis ended without a saved report. Check the application log and retry.");
        } finally {
            if (!complete) await reader.cancel();
            reader.releaseLock();
        }
    }

    form.addEventListener("submit", async event => {
        event.preventDefault();
        if (busy || !catalog || !form.reportValidity()) return;
        const changes = rows.filter(row => row.checkbox.checked).map(row => ({
            table: row.table, column: row.column,
        }));
        setBusy(true);
        results.hidden = true;
        message("Building impacted lineage from the selected columns.");
        try {
            const response = await fetch(root.dataset.analyzeUrl, {
                method: "POST",
                headers: {"Content-Type": "application/json", "Accept": "text/event-stream"},
                body: JSON.stringify({revision: catalog.revision, instructions: instructions.value.trim(), changes}),
            });
            if (!response.ok) throw new Error(await responseError(response));
            await readAnalysis(response);
        } catch (error) {
            message(error.message, true);
        } finally {
            setBusy(false);
        }
    });
    reload.addEventListener("click", () => loadCatalog());
    modelSelect.addEventListener("change", () => {
        loadModel.disabled = busy || !modelSelect.value;
    });
    modelForm.addEventListener("submit", event => {
        event.preventDefault();
        if (!busy && modelSelect.value) loadCatalog(modelSelect.value);
    });
    search.addEventListener("input", filterTables);
    instructions.addEventListener("input", updateSelection);
    loadCatalog();
})();
