// Pagina di deploy: upload dei .app (drag & drop) con avanzamento, uno alla volta in streaming.
(function () {
  "use strict";
  var form = document.getElementById("deploy-form");
  if (!form) return;
  var input = document.getElementById("file-input");
  var zone = document.getElementById("dropzone");
  var list = document.getElementById("file-list");
  var csrf = document.querySelector('meta[name="csrf-token"]').content;
  var maxBytes = parseInt(form.dataset.maxMb, 10) * 1024 * 1024;

  function changed() { htmx.trigger(form, "files-changed"); }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function makeRow(file) {
    var li = el("li", "file");
    var head = el("div", "file-head");
    head.appendChild(el("span", "file-name", file.name));
    var remove = el("button", "btn btn-ghost btn-sm", "Rimuovi");
    remove.type = "button";
    remove.addEventListener("click", function () { removeRow(li); });
    head.appendChild(remove);
    li.appendChild(head);
    var bar = el("progress", "file-progress");
    bar.max = 100; bar.value = 0;
    li.appendChild(bar);
    li.appendChild(el("small", "help file-status", "In attesa…"));
    list.appendChild(li);
    return li;
  }

  function setStatus(li, text, isError) {
    var s = li.querySelector(".file-status");
    s.textContent = text;
    s.className = (isError ? "error" : "help") + " file-status";
  }

  function fail(li, message, file) {
    li.classList.add("file-err");
    var bar = li.querySelector("progress");
    if (bar) bar.remove();
    setStatus(li, message, true);
    if (file) {
      var retry = el("button", "btn btn-sm", "Riprova");
      retry.type = "button";
      retry.addEventListener("click", function () { li.remove(); upload(file); });
      li.querySelector(".file-head").insertBefore(retry, li.querySelector(".file-head .btn"));
    }
  }

  function removeRow(li) {
    var id = li.dataset.fileId;
    li.remove();
    if (id) {
      fetch("/uploads/" + encodeURIComponent(id), { method: "DELETE", headers: { "X-CSRF-Token": csrf } });
      changed();
    }
  }

  function upload(file) {
    var li = makeRow(file);
    if (!/\.app$/i.test(file.name)) { fail(li, "Sono accettati solo file .app"); return; }
    if (file.size > maxBytes) { fail(li, "File troppo grande (massimo " + form.dataset.maxMb + " MB)"); return; }

    var xhr = new XMLHttpRequest();
    xhr.open("PUT", "/uploads?name=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.addEventListener("progress", function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round((e.loaded / e.total) * 100);
      li.querySelector("progress").value = pct;
      setStatus(li, pct < 100 ? "Caricamento " + pct + "%" : "Verifica del pacchetto…");
    });
    xhr.addEventListener("load", function () {
      if (xhr.status === 401) { window.location.href = "/login?msg=session_expired"; return; }
      if (xhr.status !== 200) { fail(li, xhr.responseText || ("Errore " + xhr.status), file); return; }
      var f = JSON.parse(xhr.responseText);
      if (list.querySelector('[data-file-id="' + f.id + '"]')) {
        li.remove(); // stesso file già in lista
        return;
      }
      li.dataset.fileId = f.id;
      li.querySelector("progress").remove();
      li.querySelector(".file-name").textContent = f.name + " " + f.version;
      setStatus(li, f.publisher + " · " + f.file + " · " + f.size_mb + " MB");
      var hidden = el("input");
      hidden.type = "hidden"; hidden.name = "file_ids"; hidden.value = f.id;
      li.appendChild(hidden);
      changed();
    });
    xhr.addEventListener("error", function () { fail(li, "Upload interrotto: controlla la connessione e riprova", file); });
    xhr.send(file);
  }

  function addFiles(files) {
    for (var i = 0; i < files.length; i++) upload(files[i]);
  }

  // Sezioni "una volta" / "ricorrente" visibili solo quando servono
  function toggleWhen() {
    var when = form.querySelector('input[name="when"]:checked').value;
    form.querySelectorAll("[data-when]").forEach(function (el) {
      el.hidden = el.dataset.when.split(" ").indexOf(when) === -1;
    });
  }
  form.querySelectorAll('input[name="when"]').forEach(function (r) { r.addEventListener("change", toggleWhen); });
  toggleWhen();

  input.addEventListener("change", function () { addFiles(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach(function (ev) {
    zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("dragover"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("dragover"); });
  });
  zone.addEventListener("drop", function (e) { addFiles(e.dataTransfer.files); });
  // Evita che un file lasciato fuori dalla zona venga aperto dal browser
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) { e.preventDefault(); });
})();
