// Log viewer: optional live tail of the log file via /log/content polling.
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 1000;
  var STORAGE_KEY = "logLiveUpdates";

  var pre = document.getElementById("log-content");
  var live = document.getElementById("log-live");
  var status = document.getElementById("log-status");
  if (!pre || !live) return;

  var accessKey = window.__ACCESS_KEY__ || "";
  var generation = pre.dataset.generation;
  var offset = pre.dataset.offset || "0";
  var timer = null;
  var inFlight = false;

  function scrollToBottom() {
    pre.scrollTop = pre.scrollHeight;
  }

  function isNearBottom() {
    return pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  }

  function setStatus(text) {
    if (status) status.textContent = text;
  }

  function contentUrl() {
    var params = new URLSearchParams({ generation: generation, offset: offset });
    if (accessKey) params.set("key", accessKey);
    return "/log/content?" + params.toString();
  }

  function poll() {
    if (inFlight) return;
    inFlight = true;
    fetch(contentUrl(), { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (chunk) {
        var follow = isNearBottom();
        if (chunk.reset) {
          pre.textContent = chunk.text;
          follow = true;
        } else if (chunk.text) {
          pre.appendChild(document.createTextNode(chunk.text));
        }
        generation = String(chunk.generation);
        offset = String(chunk.offset);
        if (follow) scrollToBottom();
        setStatus("");
      })
      .catch(function () {
        setStatus("offline — retrying");
      })
      .then(function () {
        inFlight = false;
      });
  }

  function setLive(enabled) {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (enabled) {
      poll();
      timer = setInterval(poll, POLL_INTERVAL_MS);
    } else {
      setStatus("");
    }
    try { localStorage.setItem(STORAGE_KEY, enabled ? "1" : "0"); } catch (e) { /* storage unavailable */ }
  }

  var saved = null;
  try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) { /* storage unavailable */ }
  live.checked = saved === "1";
  live.addEventListener("change", function () { setLive(live.checked); });

  scrollToBottom();
  setLive(live.checked);
})();
