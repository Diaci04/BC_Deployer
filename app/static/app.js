// Comportamenti comuni. Nessuno script inline: la CSP consente solo 'self'.
(function () {
  "use strict";

  function toast(message) {
    var el = document.createElement("div");
    el.className = "alert alert-err toast";
    el.setAttribute("role", "alert");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
  }
  window.bcdToast = toast;

  // Errori delle richieste htmx: mostra il testo restituito dal server
  document.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail.xhr;
    if (xhr.status === 401) return; // gestito con HX-Redirect verso /login
    toast(xhr.responseText || ("Errore " + xhr.status));
  });
  document.addEventListener("htmx:sendError", function () {
    toast("Server non raggiungibile");
  });

  // Nuova connessione o copia: il nome si compone da cliente ed environment, finché non lo modifichi a mano
  function autoName(form) {
    var name = form.querySelector('input[name="name"]');
    var customer = form.querySelector('input[name="customer"]');
    var env = form.querySelector('input[name="environment"]');
    if (!name || !env) return;
    var auto = name.value === "" || / \(copia\)$/.test(name.value);
    name.addEventListener("input", function () { auto = name.value === ""; });
    function update() {
      if (!auto || !env.value.trim()) return;
      name.value = [customer ? customer.value.trim() : "", env.value.trim()].filter(Boolean).join(" - ");
    }
    env.addEventListener("input", update);
    if (customer) customer.addEventListener("input", update);
  }

  // I messaggi di conferma spariscono dopo qualche secondo
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-autoname]").forEach(autoName);
    document.querySelectorAll(".flash").forEach(function (el) {
      setTimeout(function () { el.classList.add("fade"); }, 4000);
      setTimeout(function () { el.remove(); }, 4500);
    });
    // Toglie ?msg= dall'indirizzo, così ricaricando la pagina il messaggio non ricompare
    if (window.history.replaceState && /[?&]msg=/.test(location.search)) {
      var url = new URL(location.href);
      url.searchParams.delete("msg");
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
  });
})();
