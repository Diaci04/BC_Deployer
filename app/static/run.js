// Pagina di un'esecuzione: aggiornamenti live via Server-Sent Events.
(function () {
  "use strict";
  var root = document.getElementById("run");
  if (!root || root.dataset.final === "true") return;

  var FINAL = ["succeeded", "failed", "canceled", "interrupted"];
  var itemLabels = JSON.parse(root.dataset.itemLabels);
  var runLabels = JSON.parse(root.dataset.runLabels);
  var log = document.getElementById("log");
  var fmt = new Intl.DateTimeFormat("it-IT", {
    timeZone: root.dataset.tz, hour: "2-digit", minute: "2-digit", second: "2-digit"
  });

  var es = new EventSource("/runs/" + root.dataset.runId + "/events?after=" + root.dataset.lastLog);

  es.addEventListener("log", function (e) {
    var d = JSON.parse(e.data);
    var line = document.createElement("div");
    line.className = "log-line log-" + d.level;
    var t = document.createElement("time");
    t.textContent = fmt.format(new Date(d.ts));
    line.appendChild(t);
    line.appendChild(document.createTextNode(" " + d.message));
    var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.appendChild(line);
    if (atBottom) log.scrollTop = log.scrollHeight;
  });

  es.addEventListener("item", function (e) {
    var d = JSON.parse(e.data);
    var li = document.getElementById("item-" + d.id);
    if (!li) return;
    var st = li.querySelector("[data-status]");
    st.className = "status status-" + d.status;
    st.textContent = itemLabels[d.status] || d.status;
    var err = li.querySelector("[data-error]");
    err.textContent = d.error_message || "";
    err.hidden = !d.error_message;
    if (d.installed_version) li.querySelector("[data-installed]").textContent = " · installata prima: " + d.installed_version;
  });

  es.addEventListener("run", function (e) {
    var d = JSON.parse(e.data);
    var st = document.getElementById("run-status");
    st.className = "status status-" + d.status;
    st.textContent = runLabels[d.status] || d.status;
    var err = document.getElementById("run-error");
    err.textContent = d.error || "";
    err.hidden = !d.error;
    if (FINAL.indexOf(d.status) !== -1) {
      es.close();
      document.getElementById("live").hidden = true;
      document.getElementById("cancel-btn").hidden = true;
      // Ricarica per mostrare i pulsanti finali (rilancio)
      setTimeout(function () { window.location.reload(); }, 2000);
    }
  });
})();
