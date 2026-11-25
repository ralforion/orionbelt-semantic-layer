// OrionBelt BI tokens page: sign in with the identity provider (authorization
// code + PKCE, in the browser), then create, list and revoke your BI tokens
// through /v1/auth/tokens. The sign-in token is kept in memory only: a reload
// signs in again. Everything the server returns is shown as text, never HTML.
//
// Where a data source logs in as the user (config.warehouse_sign_in), a new
// token takes a warehouse sign-in along: the user signs in once more, as
// OrionBelt's own client, in a popup the server sends back to /tokens/consent,
// and the token is created with that sign-in.
"use strict";

const config = JSON.parse(document.getElementById("config").textContent);
const base = location.pathname.replace(/\/tokens\/?$/, "");
const redirectUri = location.origin + base + "/tokens";
const PENDING = "obsl-token-page-sign-in";
const POLL_MS = 1000;
let accessToken = null;
let shownId = null; // the token whose value the page shows once, if any
let creating = 0; // the latest Create; an earlier one still waiting gives up

const $ = (id) => document.getElementById(id);

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

function showNotice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
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

// The message /tokens/consent posts when the sign-in came back, or a poll's
// interval, whichever is first.
function nextSignal(ms) {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve();
    };
    const onMessage = (event) => {
      if (event.origin === location.origin && event.data && event.data.type === "obsl-consent") done();
    };
    const timer = setTimeout(done, ms);
    window.addEventListener("message", onMessage);
  });
}

// Stops an attempt a newer Create took over; its error says nothing. Checked
// after every wait: an answer that arrives late must not create a token.
function stillCurrent(attempt) {
  if (attempt !== creating) throw new Error("");
}

// Sign the user in for the warehouses in ``popup``; the finished sign-in's id.
// The server's status is what counts: an identity provider may cut the popup
// off from this page (it then looks closed while the user is still in it).
async function warehouseSignIn(popup, attempt) {
  let started;
  try {
    started = await api("POST", "/consents");
  } catch (error) {
    popup.close();
    throw error;
  }
  if (attempt !== creating) {
    popup.close();
    stillCurrent(attempt);
  }
  popup.location.href = started.authorize_url;
  showNotice(
    "Sign in in the window that opened: the token then reaches the data sources " +
      "that log in as you. If you closed it, create the token again."
  );
  const deadline = Date.now() + started.expires_in * 1000;
  try {
    while (Date.now() < deadline) {
      await nextSignal(POLL_MS);
      stillCurrent(attempt);
      const status = await api("GET", "/consents/" + encodeURIComponent(started.consent));
      stillCurrent(attempt);
      if (status.status === "finished") return started.consent;
      if (status.status === "failed") throw new Error(status.message || "The sign-in failed.");
    }
    throw new Error("The sign-in took too long: create the token again.");
  } finally {
    if (attempt === creating) showNotice("");
    if (!popup.closed) popup.close();
  }
}

async function create(event) {
  event.preventDefault();
  const form = event.target;
  const scopes = [...form.querySelectorAll("input[name=scope]:checked")].map((box) => box.value);
  const attempt = ++creating;
  const body = { label: form.label.value, scopes, days: Number(form.days.value) };
  if (config.warehouse_sign_in) {
    // Opened now, while the click still counts as the user's: a window opened
    // after waiting for the server is blocked as an unasked popup. Unnamed, so
    // a second Create gets its own and the first one's cleanup closes only its own.
    const popup = window.open("", "", "popup,width=520,height=720");
    if (!popup) throw new Error("Allow pop-ups for this page: the token's sign-in opens in one.");
    body.consent = await warehouseSignIn(popup, attempt);
    stillCurrent(attempt);
  }
  const created = await api("POST", "", body);
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
    if (error.message) showError(error.message); // an empty one: a newer Create took over
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
