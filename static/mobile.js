// WebSocket client for the /mobile summary page (issue #119).
// View-only: updates probe temperatures, output on/off dots and current price.
(function () {
  "use strict";

  var accessKey = window.__ACCESS_KEY__ || "";
  var pageAutoRefresh = Number(window.__PAGE_AUTO_REFRESH__ || 0);
  var ws = null;

  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function setConn(state) {
    // state: "live" | "offline" | "unauthorized"
    var el = document.getElementById("conn-status");
    if (!el) return;
    if (state === "unauthorized") {
      el.textContent = "unauthorized — add ?key= to the URL";
      el.className = "conn-offline";
    } else {
      el.textContent = state;
      el.className = state === "live" ? "conn-online" : "conn-offline";
    }
  }

  function stampRefresh() {
    var el = document.getElementById("last-refresh");
    if (!el) return;
    el.textContent = new Date().toTimeString().slice(0, 8);
  }

  function formatTemp(value) {
    if (value === null || value === undefined) return "N/A";
    var n = Number(value);
    if (!Number.isFinite(n)) return "N/A";
    return n.toFixed(1) + " \u00B0C";
  }

  function applySnapshot(snapshot) {
    // Temp probes
    if (snapshot.global && Array.isArray(snapshot.global.TempProbeData)) {
      snapshot.global.TempProbeData.forEach(function (probe) {
        var row = document.querySelector('#mobile-probes [data-probe-name="' + cssEscape(probe.name) + '"]');
        if (!row) return;
        var tempEl = row.querySelector('[data-probe-field="temperature"]');
        if (tempEl) tempEl.textContent = formatTemp(probe.temperature);
      });
    }

    // Outputs
    var outputs = snapshot.outputs || {};
    Object.values(outputs).forEach(function (output) {
      if (!output || !output.id) return;
      var row = document.querySelector('#mobile-outputs [data-output-id="' + cssEscape(output.id) + '"]');
      if (!row) return;

      if (typeof output.is_on === "boolean") {
        var ind = row.querySelector('[data-field="indicator"]');
        if (ind) {
          ind.className = "status-indicator " +
            (output.is_on ? "status-indicator--active" : "status-indicator--inactive");
        }
      }

      var priceEl = row.querySelector('[data-field="current_price"]');
      if (priceEl && output.current_price != null) priceEl.textContent = String(output.current_price);
    });

    stampRefresh();
  }

  function connect() {
    var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = protocol + "//" + window.location.host + "/ws";
    if (accessKey) url += "?key=" + encodeURIComponent(accessKey);
    ws = new WebSocket(url);

    ws.onopen = function () { setConn("live"); };
    ws.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === "state_update" && msg.state) applySnapshot(msg.state);
    };
    ws.onclose = function (event) {
      ws = null;
      // 1008 = policy violation: the access key was missing/invalid. Retrying is futile.
      if (event && event.code === 1008) {
        setConn("unauthorized");
        return;
      }
      setConn("offline");
      setTimeout(connect, 2000);
    };
    ws.onerror = function () { if (ws) ws.close(); };
  }

  // Periodic refresh: request a fresh snapshot over the existing socket (no full-page reload).
  if (pageAutoRefresh > 0) {
    setInterval(function () {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: "request_snapshot" })); } catch (e) { /* ignore */ }
      }
    }, pageAutoRefresh * 1000);
  }
  connect();
})();
