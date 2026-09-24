// htmx does not swap 4xx/5xx responses, so a failed request (for example the 503 returned when a
// job could not be queued) would leave the page looking as if nothing happened. Say so instead.
document.addEventListener("htmx:responseError", function (event) {
  var xhr = event.detail.xhr;
  var message = "";
  try {
    message = JSON.parse(xhr.responseText).detail || "";
  } catch (ignored) { /* not JSON */ }
  alert(message || "Request failed (" + xhr.status + ")");
});
