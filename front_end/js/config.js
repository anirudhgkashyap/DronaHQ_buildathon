/* ==========================================================================
   config.js: the ONLY file the backend team needs to edit to connect.
   ========================================================================== */
window.Atlas = window.Atlas || {};

Atlas.config = {
  /* true  = every API call is answered by js/mock-data.js (no server needed)
     false = calls go to API_BASE_URL */
  USE_MOCK: true,

  /* Base URL of the REST API. All endpoint paths in js/api.js are appended to it. */
  API_BASE_URL: "/api/v1",

  /* Auth. Pick ONE:
     - Bearer token: return the token from getAuthToken()
     - Session cookie: set SEND_COOKIES to true (server must allow credentials in CORS) */
  SEND_COOKIES: false,
  getAuthToken: function () {
    return window.localStorage.getItem("atlas_token");
  },
  /* Called when the API answers 401. Default: go to the login page. */
  onUnauthorized: function () {
    window.location.href = "/login.html";
  },

  /* Background refresh of the campaigns page, in ms. 0 turns polling off. */
  POLL_INTERVAL_MS: 30000,

  /* Page routes. Change here if the app is served under a different structure. */
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
