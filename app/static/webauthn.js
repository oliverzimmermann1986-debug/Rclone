(() => {
  "use strict";

  const errorBox = document.querySelector("[data-webauthn-error]");

  function showError(message, isError = true) {
    if (!errorBox) return;
    errorBox.textContent = message;
    errorBox.hidden = false;
    errorBox.classList.toggle("error", isError);
  }

  function clearMessage() {
    if (errorBox) errorBox.hidden = true;
  }

  function csrfToken() {
    const prefix = "rclone_sync_csrf=";
    const part = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith(prefix));
    return part ? decodeURIComponent(part.slice(prefix.length)) : "";
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body) headers.set("Content-Type", "application/json");
    if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
      const csrf = csrfToken();
      if (csrf) headers.set("X-CSRF-Token", csrf);
    }
    const response = await fetch(path, {...options, headers, credentials: "same-origin"});
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* non-JSON proxy error */ }
    if (!response.ok) {
      const detail = typeof payload.detail === "string" ? payload.detail : `Serverfehler (HTTP ${response.status})`;
      throw new Error(detail);
    }
    return payload;
  }

  function decodeBase64URL(value) {
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
    const bytes = Uint8Array.from(atob(padded), char => char.charCodeAt(0));
    return bytes.buffer;
  }

  function encodeBase64URL(buffer) {
    if (buffer === null || buffer === undefined) return null;
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function decodeCreationOptions(options) {
    options.challenge = decodeBase64URL(options.challenge);
    options.user.id = decodeBase64URL(options.user.id);
    for (const item of options.excludeCredentials || []) item.id = decodeBase64URL(item.id);
    return options;
  }

  function decodeRequestOptions(options) {
    options.challenge = decodeBase64URL(options.challenge);
    for (const item of options.allowCredentials || []) item.id = decodeBase64URL(item.id);
    return options;
  }

  function credentialJSON(credential) {
    const response = credential.response;
    const result = {
      id: credential.id,
      rawId: encodeBase64URL(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment || null,
      response: {
        clientDataJSON: encodeBase64URL(response.clientDataJSON)
      }
    };
    if (response.attestationObject) {
      result.response.attestationObject = encodeBase64URL(response.attestationObject);
      result.response.transports = typeof response.getTransports === "function" ? response.getTransports() : [];
    } else {
      result.response.authenticatorData = encodeBase64URL(response.authenticatorData);
      result.response.signature = encodeBase64URL(response.signature);
      result.response.userHandle = encodeBase64URL(response.userHandle);
    }
    return result;
  }

  async function authenticate(method, native = false, nativeChallenge = "") {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error("Dieser Browser unterstützt Passkeys und Sicherheitsschlüssel nicht.");
    }
    const start = await request("/api/webauthn/authentication/options", {
      method: "POST",
      body: JSON.stringify({method, native, native_challenge: nativeChallenge})
    });
    const credential = await navigator.credentials.get({publicKey: decodeRequestOptions(start.publicKey)});
    if (!credential) throw new Error("Die Anmeldung wurde abgebrochen.");
    return request("/api/webauthn/authentication/verify", {
      method: "POST",
      body: JSON.stringify({challenge_id: start.challenge_id, credential: credentialJSON(credential)})
    });
  }

  async function register(method) {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error("Dieser Browser unterstützt WebAuthn nicht.");
    }
    const password = document.querySelector("[data-webauthn-password]")?.value || "";
    const label = document.querySelector("[data-webauthn-label]")?.value || "";
    if (!password) throw new Error("Gib zur Bestätigung dein aktuelles Passwort ein.");
    const start = await request("/api/webauthn/registration/options", {
      method: "POST",
      body: JSON.stringify({method, label, current_password: password})
    });
    const credential = await navigator.credentials.create({publicKey: decodeCreationOptions(start.publicKey)});
    if (!credential) throw new Error("Die Registrierung wurde abgebrochen.");
    await request("/api/webauthn/registration/verify", {
      method: "POST",
      body: JSON.stringify({challenge_id: start.challenge_id, credential: credentialJSON(credential)})
    });
    const passwordInput = document.querySelector("[data-webauthn-password]");
    if (passwordInput) passwordInput.value = "";
    showError("Die sichere Anmeldeart wurde registriert.", false);
    await loadCredentials();
  }

  async function loadCredentials() {
    const target = document.querySelector("[data-webauthn-credentials]");
    if (!target) return;
    const result = await request("/api/webauthn/credentials");
    target.replaceChildren();
    if (!result.credentials.length) {
      const empty = document.createElement("p");
      empty.textContent = "Noch kein Passkey oder Sicherheitsschlüssel registriert.";
      target.append(empty);
      return;
    }
    for (const item of result.credentials) {
      const row = document.createElement("div");
      row.className = "credential";
      const text = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = item.label || (item.method === "passkey" ? "Passkey" : "Sicherheitsschlüssel");
      const details = document.createElement("small");
      const created = new Date(item.created_at * 1000).toLocaleDateString("de-DE");
      details.textContent = `${item.method === "passkey" ? "Passkey" : "Sicherheitsschlüssel"} · seit ${created}`;
      text.append(title, details);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "Entfernen";
      remove.addEventListener("click", async () => {
        const password = window.prompt("Aktuelles Passwort zur Bestätigung:");
        if (!password) return;
        try {
          await request(`/api/webauthn/credentials/${encodeURIComponent(item.id)}`, {
            method: "DELETE",
            body: JSON.stringify({current_password: password})
          });
          await loadCredentials();
        } catch (error) { showError(error.message); }
      });
      row.append(text, remove);
      target.append(row);
    }
  }

  document.querySelectorAll("[data-webauthn-action]").forEach(button => {
    button.addEventListener("click", async () => {
      clearMessage();
      button.disabled = true;
      try {
        const action = button.dataset.webauthnAction;
        const method = button.dataset.method || document.body.dataset.method;
        if (action === "login") {
          await authenticate(method, false);
          window.location.assign("/");
        } else if (action === "native-login") {
          const result = await authenticate(method, true, document.body.dataset.appChallenge || "");
          if (!result.native_exchange_token) throw new Error("Der Server hat keinen App-Anmeldecode geliefert.");
          window.location.assign(`rclonesync://webauthn?token=${encodeURIComponent(result.native_exchange_token)}`);
        } else if (action === "register") {
          await register(method);
        }
      } catch (error) {
        if (error?.name !== "NotAllowedError") showError(error?.message || "Die Aktion ist fehlgeschlagen.");
      } finally {
        button.disabled = false;
      }
    });
  });

  const loginActions = document.querySelector("[data-webauthn-login]");
  if (loginActions) {
    request("/api/webauthn/status").then(result => {
      loginActions.hidden = !result.enabled || (!result.passkey && !result.security_key);
      loginActions.querySelector('[data-method="passkey"]').hidden = !result.passkey;
      loginActions.querySelector('[data-method="security_key"]').hidden = !result.security_key;
    }).catch(() => { loginActions.hidden = true; });
  }
  if (document.body.hasAttribute("data-webauthn-security")) {
    loadCredentials().catch(error => showError(error.message));
  }
})();
