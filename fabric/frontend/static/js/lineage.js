(() => {
    const search = document.getElementById("lineageSearch");
    const status = document.getElementById("lineageFilterStatus");
    const records = [...document.querySelectorAll("[data-lineage-record]")];
    const browser = document.getElementById("lineageArtifactBrowser");
    if (browser && search && status) {
        const panel = document.getElementById("lineageNotebookDetails");
        const emptyState = panel.firstElementChild;
        const tabs = [...browser.querySelectorAll('[role="tab"]')];
        const notebookTemplates = new Map(
            [...browser.querySelectorAll("[data-lineage-notebook-template]")].map(template => [
                template.id, {
                    template,
                    searchable: template.content.textContent.toLocaleLowerCase(),
                },
            ])
        );
        const notebooks = [...browser.querySelectorAll("[data-lineage-notebook-record]")].map(record => {
            const button = record.querySelector("[data-lineage-notebook]");
            return {
                record, button, ...notebookTemplates.get(button.dataset.lineageNotebook),
            };
        });
        const pipelines = [...browser.querySelectorAll("[data-lineage-pipeline-record]")].map(record => {
            const button = record.querySelector("[data-lineage-pipeline]");
            const template = document.getElementById(button.dataset.lineagePipeline);
            const dependencies = [...template.content.querySelectorAll("[data-lineage-notebook]")].map(
                notebook => notebookTemplates.get(notebook.dataset.lineageNotebook).searchable
            );
            return {
                record, button, template,
                searchable: `${template.content.textContent} ${dependencies.join(" ")}`.toLocaleLowerCase(),
            };
        });
        const sourceRecords = records.map(record => ({
            record, searchable: record.textContent.toLocaleLowerCase(),
        }));
        let activeTab = tabs[0];
        let selectedPipeline = null;
        let selectedNotebook = null;

        const resetNotebookButtons = () => {
            browser.querySelectorAll("[data-lineage-notebook]").forEach(button => {
                button.setAttribute("aria-expanded", "false");
            });
        };

        const clearSelection = () => {
            if (selectedPipeline) selectedPipeline.button.setAttribute("aria-pressed", "false");
            resetNotebookButtons();
            selectedPipeline = null;
            selectedNotebook = null;
            panel.replaceChildren(emptyState);
        };

        const selectNotebook = button => {
            resetNotebookButtons();
            selectedNotebook = button?.dataset.lineageNotebook || null;
            const target = selectedPipeline ? panel.querySelector("[data-lineage-pipeline-tables]") : panel;
            if (button) {
                button.setAttribute("aria-expanded", "true");
                target.replaceChildren(notebookTemplates.get(selectedNotebook).template.content.cloneNode(true));
            } else if (selectedPipeline) {
                target.replaceChildren();
            } else {
                target.replaceChildren(emptyState);
            }
        };

        const filter = () => {
            const query = search.value.trim().toLocaleLowerCase();
            [...sourceRecords, ...notebooks, ...pipelines].forEach(({record, searchable}) => {
                record.hidden = !searchable.includes(query);
            });
            if (selectedPipeline?.record.hidden) clearSelection();
            panel.querySelectorAll("[data-lineage-notebook-record]").forEach(record => {
                const id = record.querySelector("[data-lineage-notebook]").dataset.lineageNotebook;
                record.hidden = !notebookTemplates.get(id).searchable.includes(query);
            });
            if (selectedNotebook && !notebookTemplates.get(selectedNotebook).searchable.includes(query)) {
                selectNotebook(null);
            }
            [pipelines, notebooks].forEach((entries, index) => {
                const tabPanel = document.getElementById(tabs[index].getAttribute("aria-controls"));
                tabPanel.querySelector("[data-lineage-no-results]").hidden =
                    entries.length === 0 || entries.some(entry => !entry.record.hidden);
            });
            const entries = activeTab === tabs[0] ? pipelines : notebooks;
            const visible = entries.filter(entry => !entry.record.hidden).length;
            status.textContent = `${visible} of ${entries.length} ${activeTab.textContent.trim().toLocaleLowerCase()} shown`;
        };

        const selectPipeline = pipeline => {
            clearSelection();
            selectedPipeline = pipeline;
            pipeline.button.setAttribute("aria-pressed", "true");
            panel.replaceChildren(pipeline.template.content.cloneNode(true));
            filter();
        };

        const activateTab = tab => {
            if (activeTab !== tab) clearSelection();
            activeTab = tab;
            tabs.forEach(item => {
                const selected = item === tab;
                item.setAttribute("aria-selected", String(selected));
                item.tabIndex = selected ? 0 : -1;
                document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
            });
            filter();
        };

        browser.addEventListener("click", event => {
            const tab = event.target.closest('[role="tab"]');
            if (tab) activateTab(tab);
            const pipelineButton = event.target.closest("[data-lineage-pipeline]");
            if (pipelineButton) {
                const pipeline = pipelines.find(item => item.button === pipelineButton);
                if (selectedPipeline === pipeline) clearSelection();
                else selectPipeline(pipeline);
            }
            const button = event.target.closest("[data-lineage-notebook]");
            if (button) {
                if (button.dataset.lineageNotebookTarget) {
                    const notebook = notebooks.find(item =>
                        item.button.dataset.lineageNotebook === button.dataset.lineageNotebookTarget
                    );
                    if (!notebook.searchable.includes(search.value.trim().toLocaleLowerCase())) {
                        search.value = "";
                    }
                    activateTab(tabs[1]);
                    selectNotebook(notebook.button);
                    notebook.button.focus({preventScroll: true});
                    notebook.button.scrollIntoView({block: "nearest"});
                } else {
                    selectNotebook(selectedNotebook === button.dataset.lineageNotebook ? null : button);
                }
            }
            const childPipeline = event.target.closest("[data-lineage-child-pipeline]");
            if (childPipeline) {
                search.value = "";
                const pipeline = pipelines.find(item => item.button.id === childPipeline.dataset.lineageChildPipeline);
                selectPipeline(pipeline);
                pipeline.button.focus();
            }
            if ((button || pipelineButton || childPipeline) && window.matchMedia("(max-width: 800px)").matches) {
                panel.scrollIntoView({block: "nearest"});
            }
        });
        tabs.forEach((tab, index) => tab.addEventListener("keydown", event => {
            let target;
            if (event.key === "ArrowRight") target = tabs[(index + 1) % tabs.length];
            if (event.key === "ArrowLeft") target = tabs[(index + tabs.length - 1) % tabs.length];
            if (event.key === "Home") target = tabs[0];
            if (event.key === "End") target = tabs[tabs.length - 1];
            if (target) {
                event.preventDefault();
                activateTab(target);
                target.focus();
            }
        }));
        search.addEventListener("input", filter);
        filter();
    }

    const screen = document.getElementById("lineageProcessingScreen");
    const refreshButton = document.getElementById("refreshLineageButton");
    const retryButton = document.getElementById("retryLineageButton");
    const processingStatus = document.getElementById("lineageProcessingStatus");
    const progress = document.getElementById("lineageProcessingProgress");
    const progressBar = progress?.querySelector("span");
    const stages = {
        repository: document.getElementById("lineageStageRepository"),
        lineage: document.getElementById("lineageStageLineage"),
        reports: document.getElementById("lineageStageReports"),
    };
    let refreshRequested = false;

    function resetProgress() {
        Object.values(stages).forEach(stage => {
            stage.dataset.state = "pending";
        });
        processingStatus.classList.remove("is-error");
        retryButton.hidden = true;
        progress.setAttribute("aria-valuenow", "0");
        progressBar.style.width = "0%";
    }

    function updateProgress(event) {
        if (stages[event.stage]) {
            stages[event.stage].dataset.state = event.state;
        }
        processingStatus.textContent = event.message;
        progress.setAttribute("aria-valuenow", String(event.progress));
        progressBar.style.width = `${event.progress}%`;
        if (event.state === "error") {
            processingStatus.classList.add("is-error");
            retryButton.hidden = false;
            refreshButton.disabled = false;
        }
        if (event.stage === "complete") {
            window.location.reload();
        }
    }

    async function processPreset(refresh) {
        refreshRequested = refresh;
        resetProgress();
        screen.hidden = false;
        refreshButton.disabled = true;
        try {
            const response = await fetch(screen.dataset.processUrl, {
                method: "POST",
                headers: {"Content-Type": "application/json", "Accept": "text/event-stream"},
                body: JSON.stringify({refresh}),
            });
            if (!response.ok) {
                const payload = await response.json().catch(() => ({}));
                throw new Error(payload.message || `Processing failed (${response.status}).`);
            }
            if (!response.body) {
                throw new Error("The server did not provide processing progress.");
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            while (true) {
                const {done, value} = await reader.read();
                buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
                const events = buffer.split("\n\n");
                buffer = events.pop();
                events.forEach(block => {
                    const data = block.split("\n")
                        .find(line => line.startsWith("data: "));
                    if (data) updateProgress(JSON.parse(data.slice(6)));
                });
                if (done) break;
            }
        } catch (error) {
            processingStatus.textContent = error.message;
            processingStatus.classList.add("is-error");
            retryButton.hidden = false;
            refreshButton.disabled = false;
        }
    }

    refreshButton?.addEventListener("click", () => processPreset(true));
    retryButton?.addEventListener("click", () => processPreset(refreshRequested));
    if (screen?.dataset.autoProcess === "true") {
        processPreset(false);
    }
})();
