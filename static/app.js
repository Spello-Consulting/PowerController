// WebSocket client: live output/temp-probe updates, connection status, auto-refresh.
(function () {
  "use strict";

  var accessKey = window.__ACCESS_KEY__ || "";
  var pageAutoRefresh = Number(window.__PAGE_AUTO_REFRESH__ || 0);
  var ws = null;
  var latestOutputsById = new Map();

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
    var now = new Date();
    el.textContent = now.toLocaleDateString() + " " + now.toTimeString().slice(0, 8);
  }

  // Update the home-page cards. No-ops on pages without those elements
  // (e.g. /system and /config).
  function applySnapshot(snapshot) {
    // Temp probes
    if (snapshot.global && Array.isArray(snapshot.global.TempProbeData)) {
      snapshot.global.TempProbeData.forEach(function (probe) {
        var row = document.querySelector('#temp-probes tr[data-probe-name="' + cssEscape(probe.name) + '"]');
        if (!row) return;
        var tempEl = row.querySelector('[data-probe-field="temperature"]');
        var timeEl = row.querySelector('[data-probe-field="last_logged_time"]');
        if (tempEl) tempEl.textContent = probe.temperature != null ? probe.temperature : "";
        if (timeEl) timeEl.textContent = probe.last_logged_time != null ? probe.last_logged_time : "";
      });
    }

    // Outputs
    var outputs = snapshot.outputs || {};
    Object.values(outputs).forEach(function (output) {
      if (!output || !output.id) return;

      latestOutputsById.set(output.id, output);
      var card = document.querySelector('[data-output-id="' + cssEscape(output.id) + '"]');
      if (!card) return;

      // Mode display
      var modeEl = card.querySelector('[data-field="mode"]');
      if (modeEl && output.mode) modeEl.textContent = String(output.mode).toUpperCase();

      // Status badge
      if (typeof output.is_on === "boolean") {
        var badge = card.querySelector(".status-badge");
        if (badge) {
          badge.className = "status-badge " + (output.is_on ? "on" : "off");
          badge.textContent = output.is_on ? "ON" : "OFF";
        }
      }

      // Reason
      var reasonEl = card.querySelector('[data-field="reason"]');
      if (reasonEl) reasonEl.textContent = output.reason || "";

      // Start/stop label + time
      var startStopLabel = card.querySelectorAll('[data-field="start_stop_label"]');
      var startStopTime = card.querySelectorAll('[data-field="start_stop_time"]');
      var isOn = !!output.is_on;
      startStopLabel.forEach(function (el) { el.textContent = isOn ? "Stop At:" : "Start At:"; });
      var timeValue = isOn ? (output.stopping_at || "") : (output.next_start_time || "");
      startStopTime.forEach(function (el) { el.textContent = timeValue; });

      // Simple field updates
      var fields = [
        "actual_energy_used", "actual_cost", "current_price",
        "target_hours", "forecast_energy_used", "forecast_price", "forecast_cost",
        "actual_hours", "total_energy_used", "average_price", "total_cost",
        "required_hours", "power_draw", "planned_hours"
      ];
      fields.forEach(function (field) {
        var elements = card.querySelectorAll('[data-field="' + field + '"]');
        elements.forEach(function (el) {
          var v = output[field];
          el.textContent = (v === null || v === undefined) ? "" : String(v);
        });
      });

      // Button active states + disable when the device is offline
      var buttons = card.querySelectorAll(".mode-button");
      buttons.forEach(function (btn) {
        if (output.mode) {
          var btnMode = btn.getAttribute("data-mode");
          btn.classList.toggle("active", btnMode === output.mode);
        }
        if (typeof output.is_online === "boolean") {
          btn.disabled = !output.is_online;
        }
      });
    });

    stampRefresh();
  }

  function parseOptionalInt(v) {
    if (typeof v !== "string") return null;
    var s = v.trim();
    if (!s) return null;
    var n = Number(s);
    if (!Number.isFinite(n) || !Number.isInteger(n)) return null;
    return n;
  }

  // Exposed for the inline onclick handlers in home.html.
  window.setMode = function (outputId, mode) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn("WebSocket not connected; cannot send command");
      return;
    }

    var revertTimeMins = null;
    if (mode === "on" || mode === "off") {
      var output = latestOutputsById.get(outputId);

      var defaultMins = null;
      if (output) {
        if (mode === "on") {
          defaultMins = output.max_app_mode_on_minutes;
        } else {
          defaultMins = (output.max_app_mode_off_minutes != null ? output.max_app_mode_off_minutes : output.max_app_mode_on_minutes);
        }
      } else {
        var card = document.querySelector('[data-output-id="' + cssEscape(outputId) + '"]');
        if (card && card.dataset) {
          if (mode === "on") {
            defaultMins = parseOptionalInt(card.dataset.maxAppModeOnMinutes);
          } else {
            defaultMins = (parseOptionalInt(card.dataset.maxAppModeOffMinutes) != null
              ? parseOptionalInt(card.dataset.maxAppModeOffMinutes)
              : parseOptionalInt(card.dataset.maxAppModeOnMinutes));
          }
        }
      }

      var defaultText = (typeof defaultMins === "number" && Number.isFinite(defaultMins)) ? String(defaultMins) : "";
      var promptText = "Minutes before reverting to AUTO (blank = no revert):";
      var input = window.prompt(promptText, defaultText);
      if (input === null) {
        return; // user cancelled
      }
      var trimmed = input.trim();
      if (trimmed !== "") {
        var n = Number(trimmed);
        if (!Number.isFinite(n) || !Number.isInteger(n) || n < 0) {
          window.alert("Please enter a whole number of minutes (0 or more), or leave blank.");
          return;
        }
        revertTimeMins = n;
      }
    }

    ws.send(JSON.stringify({
      type: "command",
      action: "set_mode",
      output_id: outputId,
      mode: mode,
      revert_time_mins: revertTimeMins
    }));
  };

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
      // 1008 = policy violation: the access key was missing/invalid. Retrying
      // with the same URL is futile, so stop instead of hammering the server.
      if (event && event.code === 1008) {
        setConn("unauthorized");
        return;
      }
      setConn("offline");
      setTimeout(connect, 2000);
    };
    ws.onerror = function () { if (ws) ws.close(); };
  }

  if (pageAutoRefresh > 0) {
    setInterval(function () { window.location.reload(); }, pageAutoRefresh * 1000);
  }
  connect();
})();
