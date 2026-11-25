// OrionBelt BI tokens page: sign in with the identity provider (authorization
// code + PKCE, in the browser), then create, list and revoke your BI tokens
// through /v1/auth/tokens. The sign-in token is kept in memory only: a reload
// signs in again. Everything the server returns is shown as text, never HTML.
"use strict";

const config = JSON.parse(document.getElementById("config").textContent);
const base = location.pathname.replace(/\/tokens\/?$/, "");
const redirectUri = location.origin + base + "/tokens";
const PENDING = "obsl-token-page-sign-in";
let accessToken = null;
let shownId = null; // the token whose value the page shows once, if any

const $ = (id) => document.getElementById(id);

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

function base64url(bytes) {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString() {
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

function claims(token) {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(part));
  } catch (error) {
    return {};
  }
}

async function signIn() {
  const verifier = randomString();
  const state = randomString();
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  sessionStorage.setItem(PENDING, JSON.stringify({ verifier, state }));
  const url = new URL(config.authorization_endpoint);
  const query = new URLSearchParams({
    response_type: "code",
    client_id: config.client_id,
    redirect_uri: redirectUri,
    scope: config.scopes,
    state,
    code_challenge: base64url(new Uint8Array(digest)),
    code_challenge_method: "S256",
  });
  // Some identity providers (Auth0) issue a token for the API only when asked.
  if (config.audience) query.set("audience", config.audience);
  url.search = query.toString();
  location.assign(url.toString());
}

async function finishSignIn(params) {
  const pending = JSON.parse(sessionStorage.getItem(PENDING) || "null");
  sessionStorage.removeItem(PENDING);
  history.replaceState(null, "", redirectUri);
  if (params.get("error")) {
    throw new Error(`Sign-in failed: ${params.get("error_description") || params.get("error")}`);
  }
  if (!pending || pending.state !== params.get("state")) {
    throw new Error("Sign-in failed: the answer does not match this page's request.");
  }
  const response = await fetch(config.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: params.get("code"),
      redirect_uri: redirectUri,
      client_id: config.client_id,
      code_verifier: pending.verifier,
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.access_token) {
    throw new Error(`Sign-in failed: ${body.error_description || body.error || response.status}`);
  }
  accessToken = body.access_token;
}

async function api(method, path, body) {
  const response = await fetch(base + "/v1/auth/tokens" + path, {
    method,
    headers: {
      Authorization: `Bearer ${accessToken}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 204) return null;
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = answer.detail;
    if (response.status === 401) {
      accessToken = null;
      render();
    }
    throw new Error(
      (detail && detail.message) || (typeof detail === "string" ? detail : `HTTP ${response.status}`)
    );
  }
  return answer;
}

function day(iso) {
  return iso ? iso.slice(0, 10) : "never";
}

async function refreshList() {
  const { tokens } = await api("GET", "");
  const rows = $("tokens").querySelector("tbody");
  rows.replaceChildren();
  for (const token of tokens) {
    const row = rows.insertRow();
    for (const value of [token.label, token.scopes.join(" "), day(token.expires_at), day(token.last_used_at)]) {
      row.insertCell().textContent = value;
    }
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "secondary";
    revoke.textContent = "Revoke";
    revoke.addEventListener("click", () => run(async () => {
      await api("DELETE", "/" + encodeURIComponent(token.id));
      if (token.id === shownId) hideCreated();
      await refreshList();
    }));
    row.insertCell().append(revoke);
  }
  $("tokens").hidden = tokens.length === 0;
  $("none").hidden = tokens.length !== 0;
}

async function create(event) {
  event.preventDefault();
  const form = event.target;
  const scopes = [...form.querySelectorAll("input[name=scope]:checked")].map((box) => box.value);
  const created = await api("POST", "", {
    label: form.label.value,
    scopes,
    days: Number(form.days.value),
  });
  $("token").textContent = created.token;
  $("created").hidden = false;
  shownId = created.id;
  form.label.value = "";
  await refreshList();
}

async function run(action) {
  showError("");
  try {
    await action();
  } catch (error) {
    showError(error.message);
  }
}

function render() {
  const signedIn = accessToken !== null;
  $("signed-in").hidden = !signedIn;
  $("signed-out").hidden = signedIn;
  $("who").hidden = !signedIn;
  if (signedIn) {
    const who = claims(accessToken);
    $("user").textContent = who.preferred_username || who.email || who.sub || "";
  } else {
    hideCreated();
  }
}

function hideCreated() {
  $("created").hidden = true;
  $("token").textContent = "";
  shownId = null;
}

$("sign-in").addEventListener("click", () => run(signIn));
$("sign-out").addEventListener("click", () => {
  accessToken = null;
  render();
});
$("create").addEventListener("submit", (event) => run(() => create(event)));
$("copy").addEventListener("click", () => run(() => navigator.clipboard.writeText($("token").textContent)));
$("create").days.value = config.default_days;
$("create").days.max = config.max_days;
$("max-days").textContent = `(at most ${config.max_days})`;

run(async () => {
  if (!config.authorization_endpoint || !config.token_endpoint) {
    throw new Error(
      "The identity provider's sign-in and token endpoints are unknown: set " +
        "OIDC_AUTHORIZATION_ENDPOINT and OIDC_TOKEN_ENDPOINT."
    );
  }
  const params = new URLSearchParams(location.search);
  if (params.has("code") || params.has("error")) {
    await finishSignIn(params);
  }
  render();
  if (accessToken !== null) await refreshList();
});
render();
