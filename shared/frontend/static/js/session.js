window.PortalSession = (() => {
    const storageKey = "model-transformer-session";
    let user = JSON.parse(document.getElementById("portalUser").textContent);

    function read() {
        return user;
    }

    function save(nextUser) {
        user = nextUser;
        localStorage.setItem(storageKey, JSON.stringify(user));
        renderHeader();
    }

    function renderHeader() {
        const area = document.getElementById("headerUserArea");
        const signIn = document.getElementById("headerSignInButton");
        const signOut = document.getElementById("headerSignOutButton");
        if (!area) return;
        area.replaceChildren();
        signIn.hidden = Boolean(user);
        signOut.hidden = !user;
        if (!user) return;

        const info = document.createElement("div");
        info.className = "header-user-info";
        const avatar = document.createElement("span");
        avatar.className = "user-avatar";
        avatar.textContent = String(user.name || "Microsoft user")
            .split(/\s+/).map((word) => word[0]).slice(0, 2).join("").toUpperCase();
        const details = document.createElement("div");
        const name = document.createElement("strong");
        name.textContent = user.name || "Microsoft user";
        const email = document.createElement("small");
        email.textContent = user.email || "";
        details.append(name, email);
        info.append(avatar, details);
        area.append(info);
    }

    async function signOut() {
        const button = document.getElementById("headerSignOutButton");
        const error = document.getElementById("headerSessionError");
        button.disabled = true;
        error.textContent = "";
        try {
            const response = await fetch("/api/logout", {
                method: "POST", headers: {"Accept": "application/json"},
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.message || "Could not sign out.");
            user = null;
            localStorage.removeItem(storageKey);
            window.location.assign("/");
        } catch (failure) {
            error.textContent = failure.message;
            button.disabled = false;
        }
    }

    document.getElementById("headerSignOutButton")?.addEventListener("click", signOut);
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) window.location.reload();
    });
    renderHeader();
    return {read, save};
})();