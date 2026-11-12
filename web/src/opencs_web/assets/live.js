// Keeps the status block live from the server's event stream. Without this script, or without
// EventSource, the page still shows the status as it was when it loaded.
"use strict";
(function () {
  var block = document.getElementById("status");
  var note = document.getElementById("live");
  if (!block || !note || !window.EventSource) {
    return;
  }
  var source = new EventSource(block.getAttribute("data-events"));
  source.addEventListener("status", function (e) {
    // The block as the server rendered it, every callsign escaped there.
    block.innerHTML = e.data;
    note.textContent = "Updated live.";
  });
  source.addEventListener("busy", function () {
    note.textContent = "Live updates are busy; trying again shortly.";
  });
  source.addEventListener("error", function () {
    note.textContent = "Live updates lost; reconnecting.";
  });
})();
