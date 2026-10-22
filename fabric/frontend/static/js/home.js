(() => {
    const addPresetButton = document.getElementById("addPresetButton");
    const presetStatus = document.getElementById("presetStatus");
    const presetGrid = document.getElementById("presetGrid");
    const workspaceSection = document.getElementById("workspaceSection");
    const workspaceSearch = document.getElementById("workspaceSearch");
    const workspaceStatus = document.getElementById("workspaceStatus");
    const workspaceList = document.getElementById("workspaceList");
    const repositorySection = document.getElementById("repositorySection");
    const repositoryStatus = document.getElementById("repositoryStatus");
    const repositorySearchField = document.getElementById("repositorySearchField");
    const repositorySearch = document.getElementById("repositorySearch");
    const repositoryList = document.getElementById("repositoryList");
    const selectionSection = document.getElementById("selectionSection");
    const selectionWorkspaceName = document.getElementById("selectionWorkspaceName");
    const selectionRepositoryName = document.getElementById("selectionRepositoryName");
    const semanticModelSearch = document.getElementById("semanticModelSearch");
    const semanticModelStatus = document.getElementById("semanticModelStatus");
    const semanticModelList = document.getElementById("semanticModelList");
    const createPresetButton = document.getElementById("createPresetButton");
    const createPresetStatus = document.getElementById("createPresetStatus");

    let workspaces = [];
    let repositories = [];
    let selectedWorkspace = null;
    let selectedRepository = null;
    let repositoryRequest = 0;
    let semanticModels = [];
    let selectedSemanticModel = null;
    let semanticModelRequest = 0;
    let isSaving = false;

    async function getJson(url, options = {}) {
        const response = await fetch(url, {
            headers: {"Accept": "application/json"},
            ...options,
        });
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.message || `Request failed with status ${response.status}.`);
        }
        return payload;
    }

    function setStatus(element, message, isError = false) {
        element.textContent = message;
        element.classList.toggle("is-error", isError);
    }

    function filterByQuery(items, query, fields) {
        const normalizedQuery = query.trim().toLocaleLowerCase();
        if (!normalizedQuery) {
            return items;
        }
        return items.filter((item) => fields.some((field) =>
            String(item[field] || "").toLocaleLowerCase().includes(normalizedQuery)
        ));
    }

    function createOption(title, detail, isSelected, onSelect) {
        const button = document.createElement("button");
        const name = document.createElement("strong");
        const metadata = document.createElement("small");

        button.type = "button";
        button.className = `selection-option${isSelected ? " is-selected" : ""}`;
        button.setAttribute("aria-pressed", String(isSelected));
        name.textContent = title;
        metadata.textContent = detail;
        button.append(name, metadata);
        button.addEventListener("click", onSelect);
        return button;
    }

    function renderWorkspaces() {
        const visible = filterByQuery(
            workspaces,
            workspaceSearch.value,
            ["displayName", "type"]
        );
        workspaceList.replaceChildren();
        visible.forEach((workspace) => {
            workspaceList.append(createOption(
                workspace.displayName,
                workspace.type || "Fabric workspace",
                selectedWorkspace?.id === workspace.id,
                () => selectWorkspace(workspace)
            ));
        });
        setStatus(
            workspaceStatus,
            visible.length
                ? `${visible.length} workspace${visible.length === 1 ? "" : "s"} available.`
                : "No workspaces match this search."
        );
    }

    function renderRepositories() {
        const visible = filterByQuery(
            repositories,
            repositorySearch.value,
            ["name", "projectName", "organization"]
        );
        repositoryList.replaceChildren();
        visible.forEach((repository) => {
            const location = [repository.organization, repository.projectName]
                .filter(Boolean)
                .join(" / ");
            repositoryList.append(createOption(
                repository.name,
                location,
                selectedRepository?.id === repository.id,
                () => selectRepository(repository, false)
            ));
        });
        setStatus(
            repositoryStatus,
            visible.length
                ? "No Fabric Git link was found. Select a repository to use for this workspace."
                : "No Azure DevOps repositories match this search.",
            false
        );
    }

    function resetSemanticModels() {
        semanticModelRequest += 1;
        semanticModels = [];
        selectedSemanticModel = null;
        semanticModelList.replaceChildren();
        semanticModelSearch.value = "";
        semanticModelSearch.disabled = true;
        createPresetButton.disabled = true;
        setStatus(createPresetStatus, "");
    }

    function renderSemanticModels() {
        const visible = filterByQuery(
            semanticModels,
            semanticModelSearch.value,
            ["name", "path"]
        );
        semanticModelList.replaceChildren();
        visible.forEach((model) => {
            semanticModelList.append(createOption(
                model.name,
                model.path,
                selectedSemanticModel?.path === model.path,
                () => {
                    if (isSaving) {
                        return;
                    }
                    selectedSemanticModel = model;
                    renderSemanticModels();
                    createPresetButton.disabled = false;
                    setStatus(createPresetStatus, "");
                }
            ));
        });
        setStatus(
            semanticModelStatus,
            !semanticModels.length
                ? "No semantic models were found in this repository branch."
                : visible.length
                    ? `${visible.length} semantic model${visible.length === 1 ? "" : "s"}. Select one to create a preset.`
                    : "No semantic models match this search."
        );
    }

    async function selectRepository(repository, connected) {
        if (isSaving) {
            return;
        }
        selectedRepository = repository;
        if (!connected) {
            renderRepositories();
        }
        resetSemanticModels();
        selectionWorkspaceName.textContent = selectedWorkspace.displayName;
        selectionRepositoryName.textContent = repository.name;
        selectionSection.hidden = false;
        selectionSection.scrollIntoView({behavior: "smooth", block: "start"});
        setStatus(semanticModelStatus, "Loading repository semantic models...");

        const requestId = semanticModelRequest;
        try {
            const payload = await getJson("/api/fabric/semantic-models", {
                method: "POST",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                body: JSON.stringify({repository}),
            });
            if (requestId !== semanticModelRequest) {
                return;
            }
            selectedRepository = payload.repository;
            semanticModels = payload.semanticModels;
            semanticModelSearch.disabled = false;
            renderSemanticModels();
        } catch (error) {
            if (requestId === semanticModelRequest) {
                setStatus(semanticModelStatus, error.message, true);
            }
        }
    }

    async function selectWorkspace(workspace) {
        if (isSaving) {
            return;
        }
        selectedWorkspace = workspace;
        selectedRepository = null;
        repositories = [];
        resetSemanticModels();
        renderWorkspaces();
        repositorySection.hidden = false;
        repositorySearchField.hidden = true;
        repositorySearch.value = "";
        repositoryList.replaceChildren();
        selectionSection.hidden = true;
        setStatus(repositoryStatus, "Checking the Fabric Git connection...");

        const requestId = ++repositoryRequest;
        try {
            const payload = await getJson(
                `/api/fabric/workspaces/${encodeURIComponent(workspace.id)}/repository`
            );
            if (requestId !== repositoryRequest) {
                return;
            }

            if (payload.connected) {
                const repository = payload.repository;
                const detail = [
                    repository.organization,
                    repository.projectName,
                    repository.branchName ? `Branch: ${repository.branchName}` : "",
                ].filter(Boolean).join(" / ");
                repositoryList.append(createOption(
                    repository.name,
                    detail,
                    true,
                    () => selectRepository(repository, true)
                ));
                setStatus(repositoryStatus, "Linked Azure DevOps repository found.");
                selectRepository(repository, true);
                return;
            }

            repositories = payload.repositories || [];
            repositorySearchField.hidden = false;
            renderRepositories();
        } catch (error) {
            if (requestId === repositoryRequest) {
                setStatus(repositoryStatus, error.message, true);
            }
        }
    }

    async function loadWorkspaces() {
        try {
            const payload = await getJson("/api/fabric/workspaces");
            workspaces = payload.workspaces || [];
            renderWorkspaces();
        } catch (error) {
            setStatus(workspaceStatus, error.message, true);
        }
    }

    function showPresetCount(count) {
        setStatus(
            presetStatus,
            count
                ? `${count} saved preset${count === 1 ? "" : "s"}.`
                : "No presets saved yet. Select a workspace, repository, and semantic model below."
        );
    }

    async function deletePreset(preset, card, button) {
        if (button.disabled || !window.confirm(
            `Delete preset "${preset.presetName}"? The shared local repository will be kept.`
        )) {
            return;
        }
        button.disabled = true;
        button.textContent = "Deleting...";
        setStatus(presetStatus, "Deleting the saved preset...");
        try {
            await getJson(`/api/fabric/presets/${encodeURIComponent(preset.presetId)}`, {
                method: "DELETE",
            });
            card.remove();
            showPresetCount(presetGrid.children.length);
            (presetGrid.querySelector(".fabric-preset-link") || addPresetButton).focus();
        } catch (error) {
            button.disabled = false;
            button.textContent = "Delete";
            setStatus(presetStatus, error.message, true);
        }
    }

    async function loadPresets() {
        try {
            const payload = await getJson("/api/fabric/presets");
            presetGrid.replaceChildren();
            payload.presets.forEach((preset) => {
                const card = document.createElement("article");
                const link = document.createElement("a");
                const name = document.createElement("strong");
                const source = document.createElement("small");
                const branch = document.createElement("small");
                const deleteButton = document.createElement("button");
                card.className = "fabric-preset-card";
                link.className = "fabric-preset-link";
                link.href = `/fabric/presets/${encodeURIComponent(preset.presetId)}`;
                name.textContent = preset.presetName;
                source.textContent = `${preset.workspace.displayName} / ${preset.repository.name}`;
                branch.textContent = `Branch: ${preset.repository.branchName}`;
                link.append(name, source, branch);
                deleteButton.type = "button";
                deleteButton.className = "fabric-action-button fabric-delete-preset-button";
                deleteButton.textContent = "Delete";
                deleteButton.setAttribute("aria-label", `Delete preset "${preset.presetName}"`);
                deleteButton.addEventListener("click", () => deletePreset(preset, card, deleteButton));
                card.append(link, deleteButton);
                presetGrid.append(card);
            });
            showPresetCount(payload.presets.length);
        } catch (error) {
            setStatus(presetStatus, error.message, true);
        }
    }

    async function createPreset() {
        if (isSaving || !selectedWorkspace || !selectedRepository || !selectedSemanticModel) {
            return;
        }
        isSaving = true;
        createPresetButton.disabled = true;
        addPresetButton.disabled = true;
        setStatus(createPresetStatus, "Preparing the local repository (cloned only once) and saving the preset...");
        try {
            const preset = await getJson("/api/fabric/presets", {
                method: "POST",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                body: JSON.stringify({
                    workspace: selectedWorkspace,
                    repository: selectedRepository,
                    semanticModel: selectedSemanticModel,
                }),
            });
            window.location.assign(`/fabric/presets/${encodeURIComponent(preset.presetId)}`);
        } catch (error) {
            isSaving = false;
            createPresetButton.disabled = false;
            addPresetButton.disabled = false;
            setStatus(createPresetStatus, error.message, true);
        }
    }

    addPresetButton.addEventListener("click", () => {
        if (isSaving) {
            return;
        }
        repositoryRequest += 1;
        resetSemanticModels();
        selectedWorkspace = null;
        selectedRepository = null;
        repositorySection.hidden = true;
        selectionSection.hidden = true;
        workspaceSearch.value = "";
        if (workspaces.length) {
            renderWorkspaces();
        } else {
            setStatus(workspaceStatus, "Loading workspaces...");
            loadWorkspaces();
        }
        workspaceSection.scrollIntoView({behavior: "smooth", block: "start"});
        workspaceSearch.focus({preventScroll: true});
    });
    workspaceSearch.addEventListener("input", renderWorkspaces);
    repositorySearch.addEventListener("input", renderRepositories);
    semanticModelSearch.addEventListener("input", renderSemanticModels);
    createPresetButton.addEventListener("click", createPreset);
    loadPresets();
    loadWorkspaces();
})();
