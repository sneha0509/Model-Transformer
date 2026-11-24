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
    const loginDialog = document.getElementById("impactCopilotLogin");
    const loginStatus = document.getElementById("impactCopilotLoginStatus");
    const loginButton = document.getElementById("startImpactCopilotLogin");
    const cancelLogin = document.getElementById("cancelImpactCopilotLogin");
    let catalog = null;
    let rows = [];
    let busy = false;
    let loginPending = false;
    const typeLabels = {
        Notebook: "Notebook", SemanticModel: "Semantic model", DataPipeline: "Pipeline",
        notebook_code: "Notebook cell", column: "Column", calculated_column: "Calculated column",
        measure: "Measure", table: "Table", calculated_table: "Calculated table",
        relationship: "Relationship", partition: "Source binding", hierarchy: "Hierarchy",
        role: "Security role", expression: "Source expression", pipeline_activity: "Pipeline activity",
        pipeline_parameter: "Pipeline parameter", pipeline_variable: "Pipeline variable",
    };
    const severityOrder = {high: 0, medium: 1, low: 2};
    const componentMetadata = new Set([
        "annotations", "extendedProperties", "lineageTag", "sourceLineageTag", "description", "displayFolder",
    ]);

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
            `${selected.length} ${selected.length === 1 ? "column" : "columns"} selected`;
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
        if (!value) analyze.textContent = "Analyze impact";
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
            heading.append(toggle, element("span", table.name, "impact-table-name"),
                element("span", `${table.columns.length}`, "impact-table-count"));
            group.append(heading);
            table.columns.forEach(column => {
                const container = element("div", undefined, "impact-column");
                const checkbox = element("input");
                checkbox.type = "checkbox";
                const label = labeledControl(` ${column.name} `, checkbox);
                container.append(label);
                if (column.dataType) container.append(element("small", column.dataType));
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
        document.getElementById("impactTableEmpty").hidden =
            !catalog || rows.some(row => !row.container.hidden);
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
            const option = element("option", model.name);
            option.value = model.id;
            modelSelect.append(option);
        });
        modelForm.hidden = models.length === 0;
        loadModel.disabled = true;
        updateSelection();
    }

    async function loadCatalog(modelId) {
        catalog = null;
        rows = [];
        tables.replaceChildren();
        document.getElementById("impactTableEmpty").hidden = true;
        setBusy(true);
        results.hidden = true;
        message("Loading tables and columns.");
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
            list.replaceChildren(...catalog.issues.map(issue => element("li", issue.message)));
            document.getElementById("impactCatalogGaps").hidden = !catalog.issues.length;
            message(`${catalog.tables.length} tables available \u00b7 ${catalog.semanticModel.name}`);
        } catch (error) {
            message(error.message, true);
        } finally {
            setBusy(false);
        }
    }

    function artifactIdentity(key) {
        const leaf = key.replaceAll("\\", "/").split("/").pop();
        const type = ["SemanticModel", "Notebook", "DataPipeline"].find(value => leaf.endsWith(`.${value}`));
        return {key, type, name: type ? leaf.slice(0, -(type.length + 1)) : leaf};
    }

    function sourceLocation(citation) {
        const file = citation.file?.replaceAll("\\", "/").split("/").pop();
        const coordinates = citation.cell !== undefined ?
            `Cell ${citation.cell}${citation.cellLine ? `, line ${citation.cellLine}` : ""}` :
            citation.line ? `Line ${citation.line}` : "";
        return [file, coordinates].filter(Boolean).join(" \u00b7 ");
    }

    function componentPreview(source, file, objectType) {
        const code = source || "";
        if (!/\.bim$/i.test(file || "") || !code.trim()) return {code, extracted: false};
        // Older reports can contain DAX excerpts rather than complete JSON entities.
        if (!/^[{"]/.test(code.trim())) return {code, extracted: false};
        let definition;
        try {
            definition = JSON.parse(code);
        } catch (error) {
            if (!(error instanceof SyntaxError)) throw error;
            return {code: "", extracted: false,
                error: "This BIM excerpt is incomplete. Expand Source references to inspect the original code."};
        }
        if (typeof definition === "string" && objectType === "table") definition = {name: definition};
        if (!definition || typeof definition !== "object" || Array.isArray(definition)) {
            return {code: "", extracted: false,
                error: "A model component could not be extracted. Rerun analysis for an entity-level preview."};
        }
        if (definition.model || definition.tables) {
            return {code: "", extracted: false,
                error: "This older report contains a full BIM definition. Rerun analysis for component-level code."};
        }
        return {code: componentProperties(definition), definition, extracted: true};
    }

    function componentProperties(value, indent = "") {
        return Object.entries(value).flatMap(([name, value]) => {
            const label = `${indent}${name}:`;
            if (name === "expression" && (typeof value === "string" ||
                Array.isArray(value) && value.every(line => typeof line === "string"))) {
                const expression = Array.isArray(value) ? value.join("\n") : value;
                return [label, ...expression.split("\n").map(line => `${indent}    ${line}`)];
            }
            if (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length) {
                return [label, componentProperties(value, `${indent}    `)];
            }
            const lines = JSON.stringify(value, null, 4).split("\n");
            return [`${label} ${lines[0]}`, ...lines.slice(1).map(line => `${indent}${line}`)];
        }).join("\n");
    }

    function focusedPreview(preview, changedProperties = new Set()) {
        if (!preview.definition) return preview;
        const entries = Object.entries(preview.definition);
        const primary = ([name]) => !componentMetadata.has(name) || changedProperties.has(name);
        return {
            ...preview,
            code: componentProperties(Object.fromEntries(entries.filter(primary))),
            metadataCode: componentProperties(Object.fromEntries(entries.filter(entry => !primary(entry)))),
        };
    }

    function referenceRanges(text, {impact, columns}, definition) {
        const ranges = [];
        const addProperty = (name, value, identity) => {
            if (typeof value !== "string" || !columns.has(identity.toLocaleLowerCase())) return;
            const prefix = `${name}: `;
            const literal = JSON.stringify(value);
            let offset = 0;
            for (const line of text.split("\n")) {
                if (line === prefix + literal) {
                    const start = offset + prefix.length;
                    ranges.push({start, end: start + literal.length, identity});
                    return;
                }
                offset += line.length + 1;
            }
        };
        if (impact.objectType === "relationship") {
            // Both BIM property views and exact TMDL endpoint declarations are supported.
            for (const side of ["from", "to"]) {
                if (definition?.[side + "Table"] && definition?.[side + "Column"]) {
                    addProperty(side + "Column", definition[side + "Column"],
                        `${definition[side + "Table"]}.${definition[side + "Column"]}`);
                }
            }
            const endpoint = /^[ \t]*(?:from|to)Column:[ \t]*('(?:[^']|'')*'|[^\s.']+)\.('(?:[^']|'')*'|[^\s']+)[ \t]*\r?$/gm;
            const unquote = name => name.startsWith("'") ? name.slice(1, -1).replaceAll("''", "'") : name;
            for (const match of text.matchAll(endpoint)) {
                const identity = `${unquote(match[1])}.${unquote(match[2])}`;
                if (columns.has(identity.toLocaleLowerCase())) {
                    const start = match.index + match[0].lastIndexOf(match[2]);
                    ranges.push({start, end: start + match[2].length, identity});
                }
            }
        } else {
            if (["column", "calculated_column"].includes(impact.objectType)) {
                for (const name of ["name", "sourceColumn"]) {
                    addProperty(name, definition?.[name], impact.objectName);
                }
            }
            if (["measure", "calculated_column", "calculated_table", "role"].includes(impact.objectType)) {
                const owner = definition?.name && impact.objectName.endsWith(`.${definition.name}`) ?
                    impact.objectName.slice(0, -definition.name.length - 1) :
                    impact.objectName.split(".").slice(0, -1).join(".");
                const masked = text.replace(/"(?:[^"]|"")*"|\/\/[^\n]*|--[^\n]*|\/\*[\s\S]*?\*\//g,
                    match => match.replace(/[^\n]/g, " "));
                const reference = /(?:(?<table>'(?:[^']|'')*'|[A-Za-z_]\w*)\s*)?(?<reference>\[(?<column>(?:[^\]]|\]\])*)\])/g;
                for (const match of masked.matchAll(reference)) {
                    const table = match.groups.table?.replace(/^'|'$/g, "").replaceAll("''", "'") || owner;
                    const identity = `${table}.${match.groups.column.replaceAll("]]", "]")}`;
                    if (!columns.has(identity.toLocaleLowerCase())) continue;
                    const start = match.index + match[0].lastIndexOf(match.groups.reference);
                    ranges.push({start, end: start + match.groups.reference.length, identity});
                }
            }
        }
        return ranges.sort((a, b) => a.start - b.start);
    }

    function highlightedCode(text, context, definition) {
        const code = element("code");
        let offset = 0;
        for (const range of context ? referenceRanges(text, context, definition) : []) {
            if (range.start < offset) continue;
            code.append(document.createTextNode(text.slice(offset, range.start)));
            const highlight = element("mark", text.slice(range.start, range.end), "impact-reference-highlight");
            highlight.title = `Affected column: ${range.identity}`;
            code.append(highlight);
            offset = range.end;
        }
        code.append(document.createTextNode(text.slice(offset)));
        return code;
    }

    function componentDiff(before, after) {
        const original = before ? before.split("\n") : [];
        const proposed = after ? after.split("\n") : [];
        let start = 0;
        while (start < original.length && start < proposed.length && original[start] === proposed[start]) start++;
        let end = 0;
        while (end < original.length - start && end < proposed.length - start &&
            original[original.length - end - 1] === proposed[proposed.length - end - 1]) end++;
        const contextStart = Math.max(0, start - 3);
        const contextEnd = Math.min(end, 3);
        const originalCount = original.length - end - contextStart + contextEnd;
        const proposedCount = proposed.length - end - contextStart + contextEnd;
        return [
            "--- Current component", "+++ Proposed component",
            `@@ -${contextStart + (originalCount ? 1 : 0)},${originalCount}` +
                ` +${contextStart + (proposedCount ? 1 : 0)},${proposedCount} @@`,
            ...original.slice(contextStart, start).map(line => ` ${line}`),
            ...original.slice(start, original.length - end).map(line => `-${line}`),
            ...proposed.slice(start, proposed.length - end).map(line => `+${line}`),
            ...original.slice(original.length - end, original.length - end + contextEnd).map(line => ` ${line}`),
        ].join("\n");
    }

    function codePanel(label, preview, context) {
        const panel = element("div", undefined, "impact-component-panel");
        if (label) panel.append(element("strong", label));
        if (preview.error) panel.append(element("p", preview.error, "impact-muted"));
        else {
            const source = element("pre");
            source.append(highlightedCode(preview.code, context, preview.definition));
            panel.append(source);
            if (preview.metadataCode) {
                const metadata = element("details", undefined, "impact-component-metadata");
                const properties = element("pre");
                properties.append(element("code", preview.metadataCode));
                metadata.append(element("summary", "Other component properties"), properties);
                panel.append(metadata);
            }
        }
        return panel;
    }

    function showReport(report) {
        const changes = report.changes || [];
        const targets = report.targets || [];
        const impacts = report.impacts;
        const gaps = report.gaps;
        const codeActions = impacts.filter(impact => impact.proposedFixes?.length).length;
        const withoutFixes = impacts.length - codeActions;
        const artifacts = new Map(report.inventory.map(item => [item.key, {
            ...item, impacts: [], targets: [],
        }]));
        [...targets, ...report.impacts].forEach(item => {
            if (!artifacts.has(item.artifactKey)) artifacts.set(item.artifactKey, {
                ...artifactIdentity(item.artifactKey), impacts: [], targets: [],
            });
        });
        targets.forEach(target => artifacts.get(target.artifactKey).targets.push(target));
        impacts.forEach(impact => artifacts.get(impact.artifactKey).impacts.push(impact));
        const affected = [...artifacts.values()].filter(item => item.impacts.length);
        const priority = artifact => Math.min(3, ...artifact.impacts.map(item => severityOrder[item.severity]));
        affected.sort((a, b) => priority(a) - priority(b) || a.name.localeCompare(b.name));
        const friendlyText = text => {
            let value = text || "";
            [...artifacts.values()].sort((a, b) => b.key.length - a.key.length).forEach(artifact => {
                value = value.replaceAll(artifact.key, artifact.name);
            });
            return value;
        };
        results.hidden = false;
        document.getElementById("impactReportSummary").textContent = affected.length ?
            `${affected.length} affected ${affected.length === 1 ? "artifact" : "artifacts"}. ` +
            `${impacts.length} impact ${impacts.length === 1 ? "finding" : "findings"}. ` +
            `${codeActions} with generated code; ${withoutFixes} without automatic fixes.` :
            gaps.length ? "Impact analysis is incomplete. See analysis gaps below." :
                "No affected artifacts were identified for the selected columns.";
        const metrics = [
            [affected.length, "Affected artifacts"],
            [impacts.length, "Impact findings"],
            [impacts.filter(item => item.severity === "high").length, "High priority"],
            [changes.length, "Selected columns"],
        ];
        document.getElementById("impactReportMetrics").replaceChildren(...metrics.map(([value, label]) => {
            const metric = element("div", undefined, "impact-metric");
            metric.append(element("span", label), element("strong", String(value)));
            if (label === "High priority" && value) metric.classList.add("is-high");
            return metric;
        }));
        document.getElementById("impactChangeCount").textContent =
            `${changes.length} ${changes.length === 1 ? "column" : "columns"}`;
        document.getElementById("impactSummaries").replaceChildren(
            element("p", report.instructions, "impact-saved-instructions"));
        document.getElementById("impactChangedColumns").replaceChildren(...changes.map(change =>
            element("span", `${change.table} \u00b7 ${change.column}`, "impact-change-chip")));

        const findings = document.getElementById("impactFindings");
        findings.replaceChildren();
        const cards = affected.map(artifact => {
            const card = element("article", undefined, "impact-artifact-card");
            const header = element("header", undefined, "impact-artifact-heading");
            const identity = element("div");
            identity.append(element("p", typeLabels[artifact.type] || "Artifact", "impact-artifact-type"),
                element("h4", artifact.name, "impact-artifact-name"));
            const location = artifact.origin === "fabric_api" ?
                ["Workspace", root.dataset.workspaceName] :
                [root.dataset.repositoryName, root.dataset.branchName];
            identity.append(element("p", location.filter(Boolean).join(" \u203a "), "impact-artifact-location"));
            const level = artifact.impacts.slice().sort((a, b) =>
                severityOrder[a.severity] - severityOrder[b.severity])[0]?.severity;
            header.append(identity, element("span", level ? `${level[0].toUpperCase()}${level.slice(1)} priority` :
                "Review dependency", `impact-priority impact-priority-${level || "review"}`));
            card.append(header);
            artifact.impacts.sort((a, b) => severityOrder[a.severity] - severityOrder[b.severity]).forEach(impact => {
                const finding = element("section", undefined, "impact-finding");
                const location = element("div", undefined, "impact-finding-location");
                location.append(element("span", "Affected entity", "impact-field-label"),
                    element("strong", `${typeLabels[impact.objectType] || "Object"} \u00b7 ${impact.objectName}`,
                        "impact-entity-name"));
                const related = changes.filter(change => impact.changeIds.includes(change.id));
                const contextColumns = related.filter(change =>
                    !["column", "calculated_column"].includes(impact.objectType) ||
                    `${change.table}.${change.column}` !== impact.objectName);
                const codeContext = {impact, columns: new Set([
                    ...related.map(change => `${change.table}.${change.column}`),
                    ...targets.filter(target => target.artifactKey === impact.artifactKey &&
                        ["column", "calculated_column"].includes(target.objectType) &&
                        target.sourceChangeIds?.some(id => impact.changeIds.includes(id))).map(target => target.objectName),
                ].map(identity => identity.toLocaleLowerCase()))};
                if (contextColumns.length) {
                    const columns = element("div", undefined, "impact-related-columns");
                    columns.append(element("span", "Selected source columns", "impact-field-label"));
                    const chips = element("div", undefined, "impact-change-chips");
                    contextColumns.forEach(change => chips.append(element("span",
                        `${change.table}.${change.column}`, "impact-change-chip")));
                    columns.append(chips);
                    location.append(columns);
                }
                finding.append(location);
                const hasFix = Boolean(impact.proposedFixes?.length);
                const description = element("dl", undefined, "impact-finding-description" +
                    (hasFix ? "" : " impact-finding-description-paired"));
                [["Impact", impact.impact], ["Recommended action", impact.recommendation]].forEach(([label, items]) => {
                    if (!items?.length) return;
                    const field = element("div");
                    const value = element("dd");
                    if (Array.isArray(items)) {
                        const list = element("ul", undefined, "impact-action-items");
                        items.forEach(item => list.append(element("li", friendlyText(item))));
                        value.append(list);
                    } else {
                        value.textContent = friendlyText(items);
                    }
                    field.append(element("dt", label), value);
                    description.append(field);
                });
                finding.append(description);
                const fixBox = element("section", undefined,
                    `impact-fix-box ${hasFix ? "is-available" : "is-unavailable"}`);
                fixBox.append(element("span", "Fix availability", "impact-field-label"));
                if (!hasFix) {
                    const blocked = element("div", undefined, "impact-code-fix");
                    blocked.append(element("h5", "No automatic fix available"), element("p",
                        friendlyText(impact.fixUnavailableReason ||
                            "No automatic fix was saved for this finding. Rerun analysis to assess fix availability.")));
                    fixBox.append(blocked);
                } else {
                    fixBox.append(element("h5", "Fix available"),
                        element("p", "AI-proposed code. Review only; no changes are applied.", "impact-muted"));
                }
                (impact.proposedFixes || []).forEach((fix, index) => {
                    const edit = element("section", undefined, "impact-code-fix");
                    edit.append(element("h5", impact.proposedFixes.length > 1 ?
                        `AI code change ${index + 1}` : "AI code change"));
                    const reference = element("p", sourceLocation(fix), "impact-reference-location");
                    reference.title = fix.file;
                    if (fix.cells?.length) reference.append(document.createTextNode(` \u00b7 Cell ${fix.cells.join(", ")}`));
                    edit.append(reference);
                    const comparison = element("div", undefined, "impact-code-comparison");
                    const original = componentPreview(fix.before, fix.file, impact.objectType);
                    const proposed = componentPreview(fix.after, fix.file, impact.objectType);
                    const changedProperties = new Set(original.definition && proposed.definition ?
                        [...new Set([...Object.keys(original.definition), ...Object.keys(proposed.definition)])]
                            .filter(name => JSON.stringify(original.definition[name]) !==
                                JSON.stringify(proposed.definition[name])) : []);
                    const before = focusedPreview(original, changedProperties);
                    const after = focusedPreview(proposed, changedProperties);
                    const extracted = before.extracted || after.extracted;
                    if (extracted) edit.append(element("p",
                        "BIM component view: extracted properties and expressions, not raw file syntax.", "impact-muted"));
                    comparison.append(codePanel("Before", before, codeContext),
                        codePanel("After (AI proposed)", after, codeContext));
                    if (fix.after === "") comparison.children[1].append(element("p", "Remove this code.", "impact-muted"));
                    edit.append(comparison, element("h6", "Unified diff"),
                        element("p", extracted ? "Component-view diff, not an apply-ready file patch." :
                            "Entity-relative snippet diff, not an apply-ready file patch.",
                            "impact-muted"));
                    const formattingOnly = extracted && original.code === proposed.code;
                    const diffText = before.error || after.error || formattingOnly ? "" :
                        extracted ? componentDiff(before.code, after.code) : fix.diff;
                    if (diffText) {
                        const diff = element("pre", undefined, "impact-code-diff");
                        const code = element("code");
                        const lines = diffText.split("\n");
                        if (lines[lines.length - 1] === "") lines.pop();
                        lines.forEach((line, index) => {
                            const className = index < 2 ? "impact-diff-header" :
                                line.startsWith("+") ? "impact-diff-added" :
                                line.startsWith("-") ? "impact-diff-removed" :
                                line.startsWith("@@") ? "impact-diff-hunk" : "impact-diff-context";
                            code.append(element("span", line, className), document.createTextNode("\n"));
                        });
                        diff.append(code);
                        edit.append(diff);
                    } else {
                        edit.append(element("p", formattingOnly ?
                            "Only source formatting changed; the extracted component properties are unchanged." :
                            "Run analysis again to generate a component diff for this older report.", "impact-muted"));
                    }
                    if (fix.rationale) edit.append(element("p", friendlyText(fix.rationale), "impact-muted"));
                    fixBox.append(edit);
                });
                finding.append(fixBox);
                if (!hasFix) {
                    const ownSources = (impact.sourceCode || []).filter(source =>
                        source.objectType === impact.objectType && source.objectName === impact.objectName);
                    const sources = ownSources.length ? ownSources :
                        impact.evidence.filter(citation => citation.excerpt).map(citation => ({
                            ...citation, source: citation.excerpt,
                            objectType: impact.objectType, objectName: impact.objectName,
                        }));
                    if (sources.length) {
                        const current = element("section", undefined, "impact-current-code");
                        current.append(element("h5", "Current Code"));
                        const previews = sources.map(source =>
                            focusedPreview(componentPreview(source.source, source.file, source.objectType)));
                        const components = element("div", undefined, "impact-current-components" +
                            (previews.length > 1 ? " impact-current-components-paired" : ""));
                        previews.forEach(preview => {
                            components.append(codePanel(null, preview, codeContext));
                        });
                        current.append(components);
                        finding.append(current);
                    }
                }
                if (impact.evidence.length) {
                    const source = element("details", undefined, "impact-finding-details impact-source-reference");
                    source.append(element("summary", "Source references"));
                    impact.evidence.forEach(citation => {
                        source.append(element("p", sourceLocation(citation), "impact-reference-location"),
                            element("pre", citation.excerpt));
                    });
                    finding.append(source);
                }
                card.append(finding);
            });
            findings.append(card);
            return {card, type: artifact.type};
        });
        if (!affected.length) findings.append(element("p",
            gaps.length ? "Impact analysis is incomplete for the targets listed below." :
                "No concrete impacts were identified in the supplied source.",
            "impact-empty-state"));
        const filters = document.getElementById("impactArtifactFilters");
        filters.replaceChildren();
        const options = [["all", "All artifacts"], ["SemanticModel", "Semantic models"],
            ["Notebook", "Notebooks"], ["DataPipeline", "Pipelines"]];
        options.filter(([type]) => type === "all" || cards.some(item => item.type === type)).forEach(([type, label]) => {
            const button = element("button", `${label} (${type === "all" ? cards.length :
                cards.filter(item => item.type === type).length})`, "impact-filter-button");
            button.type = "button";
            button.setAttribute("aria-pressed", String(type === "all"));
            button.addEventListener("click", () => {
                filters.querySelectorAll("button").forEach(control =>
                    control.setAttribute("aria-pressed", String(control === button)));
                cards.forEach(item => { item.card.hidden = type !== "all" && item.type !== type; });
            });
            filters.append(button);
        });
        filters.hidden = !cards.length;
        const notes = document.getElementById("impactReportGaps");
        notes.replaceChildren(...gaps.map(gap => {
            const note = element("li");
            const artifact = artifacts.get(gap.file);
            if (artifact) note.append(element("strong", `${typeLabels[artifact.type] || "Artifact"} \u00b7 ${artifact.name}: `));
            note.append(element("span", friendlyText(gap.message)));
            return note;
        }));
        const reviewNotes = document.getElementById("impactReviewNotes");
        reviewNotes.hidden = !gaps.length;
        reviewNotes.open = false;
        const notice = document.getElementById("impactReviewNotice");
        notice.hidden = !gaps.length;
        notice.textContent = `${gaps.length} analysis ${gaps.length === 1 ? "gap" : "gaps"}.`;
        const unaffected = (report.unaffectedTargets || []).filter(target => !report.impacts.some(impact =>
            impact.artifactKey === target.artifactKey && impact.objectType === target.objectType &&
            impact.objectName === target.objectName));
        renderConnections(report.impactLineage || {nodes: [], edges: []}, artifacts, unaffected);
        document.getElementById("impactResultsHeading").focus();
    }

    function renderConnections(lineage, artifacts, unaffected) {
        const names = new Map(lineage.nodes.filter(node => !unaffected.some(target =>
            node.artifactKey === target.artifactKey && node.objectType === target.objectType &&
            node.objectName === target.objectName)).map(node => {
            const artifact = artifacts.get(node.artifactKey);
            const label = artifact ? `${typeLabels[artifact.type] || "Artifact"} \u00b7 ${artifact.name}` : "";
            return [node.id, node.objectType === "artifact" ? label || node.objectName :
                [label, node.objectName].filter(Boolean).join(" \u203a ")];
        }));
        const connections = element("table", undefined, "impact-lineage-table");
        const header = element("tr");
        ["Source", "Connection", "Affected location"].forEach(text => {
            const cell = element("th", text);
            cell.setAttribute("scope", "col");
            header.append(cell);
        });
        const head = element("thead");
        head.append(header);
        const body = element("tbody");
        const connectionLabels = {
            depends_on: "Depends on", invokes: "Invokes", reads: "Reads from", writes: "Writes to",
            seed: "Selected change", references: "References", derives_from: "Derived from",
            table_lineage: "Table dependency", model_binding: "Source binding",
        };
        lineage.edges.filter(edge => edge.kind !== "contains").forEach(edge => {
            const from = names.get(edge.source);
            const to = names.get(edge.target);
            if (!from || !to) return;
            const row = element("tr");
            [from, connectionLabels[edge.kind] || "Related to", to].forEach(text => row.append(element("td", text)));
            body.append(row);
        });
        connections.append(head, body);
        document.getElementById("impactLineage").replaceChildren(body.children.length ? connections :
            element("p", "No additional dependency connections to display.", "fabric-status"));
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
                    if (event.state === "error") {
                        const error = new Error(event.message);
                        error.requiresCopilotLogin = event.requiresCopilotLogin === true;
                        throw error;
                    }
                    if (event.state === "complete") {
                        showReport(event.report);
                        complete = true;
                        message("Analysis complete. Your report is ready.");
                    } else {
                        const artifactProgress = event.message?.match(/artifact (\d+) of (\d+): (.+)/);
                        message(artifactProgress ? `Reviewing ${artifactProgress[3]} \u00b7 ${artifactProgress[1]} of ${artifactProgress[2]}` :
                            "Reviewing affected artifacts and dependencies.");
                    }
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
        analyze.textContent = "Analyzing\u2026";
        results.hidden = true;
        message("Analyzing the selected column changes.");
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
            if (error.requiresCopilotLogin) {
                loginStatus.textContent = "Sign in to continue. Analysis will retry automatically after authorization.";
                loginStatus.classList.remove("is-error");
                loginDialog.showModal();
            }
        } finally {
            setBusy(false);
        }
    });
    loginButton.addEventListener("click", async () => {
        if (loginPending) return;
        loginPending = true;
        loginButton.disabled = true;
        cancelLogin.disabled = true;
        setBusy(true);
        loginStatus.classList.remove("is-error");
        loginStatus.textContent = "Complete sign-in in your browser. This window will close when you are connected.";
        message("Waiting for GitHub Copilot browser sign-in.");
        let retryAnalysis = false;
        try {
            const response = await fetch(root.dataset.copilotLoginUrl, {
                method: "POST", headers: {"Accept": "application/json"},
            });
            if (!response.ok) throw new Error(await responseError(response));
            const payload = await response.json();
            if (payload.authenticated !== true) throw new Error("GitHub Copilot sign-in did not complete.");
            loginDialog.close();
            retryAnalysis = true;
        } catch (error) {
            loginStatus.textContent = error.message;
            loginStatus.classList.add("is-error");
            message(error.message, true);
        } finally {
            loginPending = false;
            loginButton.disabled = false;
            cancelLogin.disabled = false;
            setBusy(false);
        }
        if (retryAnalysis) form.requestSubmit();
    });
    cancelLogin.addEventListener("click", () => loginDialog.close());
    loginDialog.addEventListener("cancel", event => {
        if (loginPending) event.preventDefault();
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
    document.getElementById("impactReportDownload").addEventListener("click", () => window.print());
    let printDetails = [];
    window.addEventListener("beforeprint", () => {
        if (printDetails.length) return;
        printDetails = [...results.querySelectorAll("details")].map(detail => ({detail, open: detail.open}));
        printDetails.forEach(({detail}) => {
            if (!detail.classList.contains("impact-source-reference") &&
                !detail.classList.contains("impact-component-metadata")) detail.open = true;
        });
    });
    window.addEventListener("afterprint", () => {
        printDetails.forEach(({detail, open}) => { detail.open = open; });
        printDetails = [];
    });
    loadCatalog();
})();
