/* Minimal vanilla helpers for the admin dashboard. No dependencies. */
(function () {
  "use strict";

  /* ── Toast ──────────────────────────────────────────────────────────────
     showToast(message, kind) where kind in {success, error, info}. */
  function showToast(message, kind) {
    var el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.className = "toast " + (kind || "info") + " show";
    window.clearTimeout(el._t);
    el._t = window.setTimeout(function () {
      el.className = "toast " + (kind || "info");
    }, 3000);
  }

  /* ── Confirmable POST ────────────────────────────────────────────────────
     Builds a hidden form and submits it after an optional confirm() prompt.
     Used for status changes / destructive actions so we never POST via GET
     links and always get a server-rendered redirect back.
       postAction(url, fields, confirmMsg)
  */
  function postAction(url, fields, confirmMsg) {
    if (confirmMsg && !window.confirm(confirmMsg)) return false;
    var form = document.createElement("form");
    form.method = "POST";
    form.action = url;
    form.style.display = "none";
    fields = fields || {};
    Object.keys(fields).forEach(function (k) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = k;
      input.value = fields[k];
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
    return true;
  }

  /* Wire up declarative confirm-on-submit forms: <form data-confirm="..."> */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form && form.dataset && form.dataset.confirm) {
      if (!window.confirm(form.dataset.confirm)) {
        e.preventDefault();
      }
    }
  });

  /* Wire up buttons that trigger a confirmable POST without a wrapping form:
     <button data-post="/url" data-confirm="..." data-field-status="banned"> */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-post]");
    if (!btn) return;
    e.preventDefault();
    var fields = {};
    Object.keys(btn.dataset).forEach(function (k) {
      if (k.indexOf("field") === 0 && k.length > 5) {
        var name = k.charAt(5).toLowerCase() + k.slice(6);
        fields[name] = btn.dataset[k];
      }
    });
    postAction(btn.dataset.post, fields, btn.dataset.confirm);
  });

  /* Rows with data-href act as links (whole-row navigation). */
  document.addEventListener("click", function (e) {
    var row = e.target.closest("tr[data-href]");
    if (!row) return;
    if (e.target.closest("a, button, form, input")) return; // let real controls work
    window.location.href = row.dataset.href;
  });

  window.dash = { showToast: showToast, postAction: postAction };
})();
