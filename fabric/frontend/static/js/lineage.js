(() => {
    const search = document.getElementById("lineageSearch");
    const status = document.getElementById("lineageFilterStatus");
    const records = [...document.querySelectorAll("[data-lineage-record]")];
    if (search && status) {
        const searchable = records.map(record => record.textContent.toLocaleLowerCase());
        const filter = () => {
            const query = search.value.trim().toLocaleLowerCase();
            let visible = 0;
            records.forEach((record, index) => {
                record.hidden = !searchable[index].includes(query);
                if (!record.hidden) visible += 1;
            });
            status.textContent = `${visible} of ${records.length} mapping records shown`;
        };
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
