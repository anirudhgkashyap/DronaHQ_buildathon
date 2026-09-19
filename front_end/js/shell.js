/* ==========================================================================
   shell.js: behaviour shared by every page.
   - Atlas.fmt   formatting helpers (escape, numbers, relative time)
   - Atlas.ui    confirm dialog and toasts
   - Global kill switch in the sidebar, the banner slot, the user avatar
   ========================================================================== */
window.Atlas = window.Atlas || {};

(function (A) {
  "use strict";

  /* ---------- Formatting ---------- */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(n) { return Number(n || 0).toLocaleString("en-US"); }
  function signed(n) {
    n = Number(n || 0);
    return (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toLocaleString("en-US");
  }
  function pct(part, whole) {
    if (!whole) return "–";
    var p = (part / whole) * 100;
    return (p < 10 ? p.toFixed(1) : Math.round(p)) + "%";
  }
  function timeAgo(iso) {
    if (!iso) return "";
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 45) return "just now";
    var m = Math.round(s / 60);
    if (m < 60) return m + " min ago";
    var h = Math.round(m / 60);
    if (h < 24) return h + " hr ago";
    var d = Math.round(h / 24);
    if (d < 7) return d + (d === 1 ? " day ago" : " days ago");
    return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }
  A.fmt = { esc: esc, num: num, signed: signed, pct: pct, timeAgo: timeAgo };

  /* ---------- Confirm dialog ---------- */
  var dlg;
  function ensureDialog() {
    if (dlg) return dlg;
    dlg = document.createElement("dialog");
    dlg.className = "dialog";
    dlg.innerHTML =
      '<form method="dialog" class="dialog__body">' +
      '<h2 class="dialog__title"></h2><p class="dialog__text"></p>' +
      '<div class="dialog__actions">' +
      '<button type="submit" value="cancel" class="btn btn--secondary" data-cancel>Cancel</button>' +
      '<button type="submit" value="ok" class="btn" data-ok></button>' +
      "</div></form>";
    document.body.appendChild(dlg);
    return dlg;
  }

  /* Resolves true when the user confirms, false otherwise.
     tone: "primary" | "warn" | "danger" */
  function confirmDialog(o) {
    return new Promise(function (resolve) {
      var d = ensureDialog();
      d.querySelector(".dialog__title").textContent = o.title;
      d.querySelector(".dialog__text").textContent = o.message;
      var ok = d.querySelector("[data-ok]");
      ok.textContent = o.confirmLabel || "Confirm";
      ok.className = "btn btn--" + (o.tone || "primary");
      d.returnValue = "";
      d.addEventListener("close", function () { resolve(d.returnValue === "ok"); }, { once: true });
      d.showModal();
      (o.tone === "danger" || o.tone === "warn" ? d.querySelector("[data-cancel]") : ok).focus();
    });
  }

  /* ---------- Toasts ---------- */
  var toastRegion;
  function toast(message, o) {
    o = o || {};
    if (!toastRegion) {
      toastRegion = document.createElement("div");
      toastRegion.className = "toasts";
      toastRegion.setAttribute("role", "status");
      toastRegion.setAttribute("aria-live", "polite");
      document.body.appendChild(toastRegion);
    }
    var el = document.createElement("div");
    el.className = "toast" + (o.tone === "error" ? " toast--error" : "");
    el.innerHTML = "<span>" + esc(message) + "</span>" +
      (o.action ? '<a class="toast__action" href="' + esc(o.action.href) + '">' + esc(o.action.label) + "</a>" : "");
    toastRegion.appendChild(el);
    setTimeout(function () { el.remove(); }, o.duration || (o.tone === "error" ? 7000 : 4500));
  }
  A.ui = { confirm: confirmDialog, toast: toast };

  /* ---------- Global kill switch ---------- */
  var ksState = { engaged: false };

  function renderKillSwitch() {
    var box = document.querySelector("[data-killswitch]");
    var slot = document.querySelector('[data-slot="platform-banner"]');
    if (box) {
      box.classList.toggle("is-engaged", ksState.engaged);
      box.querySelector("[data-ks-title]").textContent = ksState.engaged ? "Platform stopped" : "Global kill switch";
      box.querySelector("[data-ks-text]").textContent = ksState.engaged
        ? "Nothing is sending anywhere."
        : "Stops every agent on every campaign and channel.";
      var btn = box.querySelector("[data-ks-button]");
      btn.textContent = ksState.engaged ? "Resume platform" : "Stop all activity";
      btn.className = "btn btn--sm " + (ksState.engaged ? "btn--light" : "btn--danger");
    }
    if (slot) {
      slot.innerHTML = ksState.engaged
        ? '<div class="banner" role="alert"><div><strong>All autonomous activity is stopped.</strong>' +
          "<p>No campaign is sending on any channel. Campaign statuses are unchanged.</p></div>" +
          '<button type="button" class="btn btn--light btn--sm" data-ks-button>Resume platform</button></div>'
        : "";
    }
    document.dispatchEvent(new CustomEvent("atlas:killswitch", { detail: ksState }));
  }

  async function toggleKillSwitch() {
    var stopping = !ksState.engaged;
    var ok = await confirmDialog(stopping
      ? { title: "Stop all activity?", message: "Every agent stops on every campaign and channel, right now. Campaign statuses and data are kept. You'll need to resume the platform to start again.", confirmLabel: "Stop all activity", tone: "danger" }
      : { title: "Resume the platform?", message: "Live campaigns will start acting again on their configured channels.", confirmLabel: "Resume platform", tone: "primary" });
    if (!ok) return;
    try {
      ksState = await A.api.setKillSwitch(stopping);
      renderKillSwitch();
      toast(stopping ? "All activity stopped." : "Platform resumed.");
    } catch (e) {
      toast(e.message, { tone: "error" });
    }
  }

  async function init() {
    document.addEventListener("click", function (e) {
      if (e.target.closest("[data-ks-button]")) toggleKillSwitch();
    });
    try { ksState = await A.api.getKillSwitch(); renderKillSwitch(); } catch (e) { /* leave default state */ }
    try {
      var me = await A.api.getMe();
      var av = document.querySelector("[data-user-avatar]");
      if (av) { av.textContent = me.initials; av.title = me.name; }
    } catch (e) { /* avatar stays empty */ }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(window.Atlas);
