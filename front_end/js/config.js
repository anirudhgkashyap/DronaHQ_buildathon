/* ==========================================================================
   config.js: the ONLY file the backend team needs to edit to connect.
   ========================================================================== */
window.Atlas = window.Atlas || {};

Atlas.config = {
  USE_MOCK: false,
  API_BASE_URL: (function () {
    return window.ATLAS_API_URL || "/api/v1";
  })(),
  SEND_COOKIES: false,
  getAuthToken: function () {
    return window.localStorage.getItem("atlas_token");
  },
  onUnauthorized: function () {
    window.location.href = "/login.html";
  },
  POLL_INTERVAL_MS: 30000,
  ROUTES: {
    campaigns: "campaigns.html",
    campaign: function (id) { return "campaign-detail.html?id=" + encodeURIComponent(id); },
    edit: function (id) { return "campaign-new.html?id=" + encodeURIComponent(id); },
    review: function (id) { return "campaign-new.html?id=" + encodeURIComponent(id) + "&step=review"; },
    create: "campaign-new.html",
    compare: function (ids) { return "campaign-compare.html?ids=" + ids.map(encodeURIComponent).join(","); },
    approval: function (id) { return "conversations.html?approval=" + encodeURIComponent(id); },
    approvals: "conversations.html?filter=approvals"
  }
};
