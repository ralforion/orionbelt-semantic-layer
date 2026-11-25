// The identity provider sent the warehouse sign-in popup back here, and the
// server has redeemed its code. Tell the BI tokens page, which also polls the
// sign-in's status (an identity provider may cut the popup off from it), and
// close.
const outcome = JSON.parse(document.getElementById("outcome").textContent);
const message = document.getElementById("message");

message.textContent = outcome.error
  ? outcome.error
  : "Signed in. You can close this window and return to the BI tokens page.";
message.className = outcome.error ? "error" : "";

if (window.opener && !window.opener.closed) {
  window.opener.postMessage({ type: "obsl-consent", ...outcome }, location.origin);
}
// The page goes on from the server's status; this window has done its part.
if (!outcome.error) window.close();
