(function () {
  "use strict";
  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  let mode = "login";
  let busy = false;

  const els = {
    subtitle: document.getElementById("subtitle"),
    form: document.getElementById("authForm"),
    identifierField: document.getElementById("identifierField"),
    identifier: document.getElementById("identifier"),
    emailField: document.getElementById("emailField"),
    email: document.getElementById("email"),
    usernameField: document.getElementById("usernameField"),
    username: document.getElementById("username"),
    password: document.getElementById("password"),
    confirmField: document.getElementById("confirmField"),
    confirm: document.getElementById("confirm"),
    error: document.getElementById("error"),
    notice: document.getElementById("notice"),
    trackingField: document.getElementById("trackingField"),
    trackingConsent: document.getElementById("trackingConsent"),
    submitBtn: document.getElementById("submitBtn"),
    switchPrompt: document.getElementById("switchPrompt"),
    toggleMode: document.getElementById("toggleMode"),
  };

  function showError(msg) {
    els.error.textContent = msg || "";
    els.error.style.display = msg ? "block" : "none";
  }
  function showNotice(msg) {
    els.notice.textContent = msg || "";
    els.notice.style.display = msg ? "block" : "none";
  }
  function setBusy(v) {
    busy = v;
    els.submitBtn.disabled = v;
    els.submitBtn.textContent = v ? "Please wait…" : (mode === "register" ? "Create account" : "Sign in");
  }
  function applyMode() {
    const isRegister = mode === "register";
    els.subtitle.textContent = isRegister ? "Create an account to start chatting." : "Sign in to pick up where you left off.";
    els.identifierField.style.display = isRegister ? "none" : "block";
    els.emailField.style.display = isRegister ? "block" : "none";
    els.usernameField.style.display = isRegister ? "block" : "none";
    els.confirmField.style.display = isRegister ? "block" : "none";
    els.trackingField.style.display = isRegister ? "flex" : "none";
    els.password.autocomplete = isRegister ? "new-password" : "current-password";
    els.submitBtn.textContent = isRegister ? "Create account" : "Sign in";
    els.switchPrompt.textContent = isRegister ? "Already have an account?" : "New here?";
    els.toggleMode.textContent = isRegister ? "Sign in" : "Create an account";
  }

  els.toggleMode.addEventListener("click", function (e) {
    e.preventDefault();
    mode = mode === "login" ? "register" : "login";
    els.confirm.value = "";
    els.trackingConsent.checked = false;
    showError("");
    showNotice("");
    applyMode();
  });

  els.form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (busy) return;

    const password = els.password.value;
    const confirm = els.confirm.value;

    let payload;
    if (mode === "register") {
      const email = els.email.value.trim();
      const username = els.username.value.trim();

      if (!email || !username || !password) {
        showError("Enter your email, a username, and a password.");
        showNotice("");
        return;
      }
      if (!EMAIL_RE.test(email)) {
        showError("Enter a valid email address.");
        showNotice("");
        return;
      }
      if (password !== confirm) {
        showError("Passwords don't match.");
        showNotice("");
        return;
      }
      if (!els.trackingConsent.checked) {
        showError("Please acknowledge how your email is used before continuing.");
        showNotice("");
        return;
      }
      payload = { identifier: email, username: username, email: email, password: password };
    } else {
      const id = els.identifier.value.trim();
      if (!id || !password) {
        showError("Enter your username or email, and a password.");
        showNotice("");
        return;
      }
      payload = { identifier: id, username: null, email: null, password: password };
    }

    const endpoint = mode === "register" ? "/api/register" : "/api/login";
    setBusy(true);
    showError("");
    showNotice("");

    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (res) {
        return res.json().then(function (data) { return { ok: res.ok, data: data }; })
          .catch(function () { return { ok: res.ok, data: {} }; });
      })
      .then(function (result) {
        const ok = result.ok, data = result.data;
        if (ok) {
          if (mode === "register" && data && data.pending) {
            setBusy(false);
            mode = "login";
            applyMode();
            els.password.value = "";
            els.confirm.value = "";
            els.identifier.value = els.email.value;
            showNotice("Account created. You can sign in now.");
            return;
          }
          window.location.href = "/";
          return;
        }
        setBusy(false);
        showError((data && (data.detail || data.error)) || "Sign in failed. Check your details.");
      })
      .catch(function () {
        setBusy(false);
        showError("Can't reach the server. Is the backend running?");
      });
  });

  applyMode();
})();
