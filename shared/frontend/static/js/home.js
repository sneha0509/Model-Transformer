(() => {
    const prompt = document.getElementById("signInPrompt");
    const button = document.getElementById("signInPromptButton");
    const buttonText = document.getElementById("signInPromptButtonText");
    const error = document.getElementById("signInPromptError");
    const picker = document.getElementById("dashboardPicker");
    const headerButton = document.getElementById("headerSignInButton");

    async function signIn() {
        error.textContent = "";
        button.disabled = true;
        headerButton.disabled = true;
        buttonText.textContent = "Signing in...";
        try {
            const response = await fetch("/api/login", {
                method: "POST", headers: {"Accept": "application/json"},
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.message || "Browser sign-in failed.");
            PortalSession.save(data.user);
            prompt.hidden = true;
            picker.hidden = false;
        } catch (failure) {
            error.textContent = failure.message;
        } finally {
            button.disabled = false;
            headerButton.disabled = false;
            buttonText.textContent = "Sign in with Microsoft";
        }
    }

    button.addEventListener("click", signIn);
    headerButton.addEventListener("click", signIn);
})();