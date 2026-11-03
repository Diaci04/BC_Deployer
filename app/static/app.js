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

  // Nuova connessione o copia: il nome si compone da cliente ed environment, finché non lo modifichi a mano.
  // Gli ascoltatori sono sul form perché il campo environment viene sostituito (testo ↔ menu a tendina).
  function autoName(form) {
    var name = form.querySelector('input[name="name"]');
    if (!name) return;
    var auto = name.value === "" || / \(copia\)$/.test(name.value);
    function update() {
      var env = form.elements.environment, customer = form.elements.customer;
      if (!auto || !env || !env.value.trim()) return;
      name.value = [customer ? customer.value.trim() : "", env.value.trim()].filter(Boolean).join(" - ");
    }
    form.addEventListener("input", function (e) {
      if (e.target === name) auto = name.value === "";
      else if (e.target.name === "environment" || e.target.name === "customer") update();
    });
    form.addEventListener("change", function (e) {
      if (e.target.name === "environment") update();
    });
  }

  // Scegliendo l'environment dal menu, "produzione" si spunta in base al tipo letto da Business Central
  document.addEventListener("change", function (e) {
    var opt = e.target.name === "environment" && e.target.selectedOptions && e.target.selectedOptions[0];
    if (!opt || !opt.dataset.production || !e.target.form) return;
    var prod = e.target.form.elements.is_production;
    if (prod) prod.checked = opt.dataset.production === "true";
  });

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
