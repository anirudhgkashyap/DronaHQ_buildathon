/* ==========================================================================
   api.js: every network call in the app lives here. Pages never call fetch().
   ========================================================================== */
window.Atlas = window.Atlas || {};

(function (A) {
  var cfg = A.config;

  function ApiError(message, opts) {
    var e = new Error(message);
    e.name = "ApiError";
    e.status = opts && opts.status;
    e.code = opts && opts.code;
    return e;
  }

  function buildQuery(query) {
    var qs = new URLSearchParams();
    Object.keys(query || {}).forEach(function (k) {
      var v = query[k];
      if (v !== undefined && v !== null && v !== "") qs.set(k, v);
    });
    var s = qs.toString();
    return s ? "?" + s : "";
  }

  async function request(method, path, opts) {
    opts = opts || {};
    var url = path + buildQuery(opts.query);

    if (cfg.USE_MOCK) return A.mock.handle(method, url, opts.body);

    var headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    var token = cfg.getAuthToken && cfg.getAuthToken();
    if (token) headers.Authorization = "Bearer " + token;

    var res;
    try {
      res = await fetch(cfg.API_BASE_URL + url, {
        method: method,
        headers: headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        credentials: cfg.SEND_COOKIES ? "include" : "same-origin"
      });
    } catch (e) {
      if (A.mock && !cfg.USE_MOCK) {
        console.warn("[Atlas] Server unreachable — falling back to mock for: " + method + " " + url);
        return A.mock.handle(method, url, opts.body);
      }
      throw ApiError("Can't reach the server. Check your connection and try again.", { code: "network_error" });
    }

    if (res.status === 401 && cfg.onUnauthorized) cfg.onUnauthorized();

    var payload = null;
    if (res.status !== 204) {
      try { payload = await res.json(); } catch (e) { payload = null; }
    }
    if (!res.ok) {
      var err = payload && payload.error;
      throw ApiError((err && err.message) || "Something went wrong (" + res.status + "). Try again.", {
        status: res.status, code: err && err.code
      });
    }
    return payload;
  }

  var ACTIONS = ["pause", "resume", "complete", "archive", "duplicate"];

  A.api = {
    ApiError: ApiError,
    request: request,
    getMe: function () { return request("GET", "/me"); },
    listCampaigns: function () { return request("GET", "/campaigns"); },
    getSummary: function () { return request("GET", "/campaigns/summary"); },
    campaignAction: function (id, action) {
      if (ACTIONS.indexOf(action) === -1) throw new Error("Unknown campaign action: " + action);
      return request("POST", "/campaigns/" + encodeURIComponent(id) + "/" + action);
    },
    listApprovals: function (o) {
      return request("GET", "/approvals", { query: { status: "pending", limit: (o && o.limit) || 5 } });
    },
    listEvents: function (o) {
      return request("GET", "/events", { query: { limit: (o && o.limit) || 5 } });
    },
    getKillSwitch: function () { return request("GET", "/platform/kill-switch"); },
    setKillSwitch: function (engaged) {
      return request("PUT", "/platform/kill-switch", { body: { engaged: !!engaged } });
    }
  };
})(window.Atlas);
