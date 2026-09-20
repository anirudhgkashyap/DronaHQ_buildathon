/* ==========================================================================
   mock-data.js: in-memory fake backend. Used only when config.USE_MOCK = true.
   It answers the exact same paths and JSON shapes as the real API (see README),
   so what works here works against the backend once it matches the contract.
   Delete this file and its <script> tag when the backend is live.
   ========================================================================== */
window.Atlas = window.Atlas || {};

(function (A) {
  var now = Date.now();
  function ago(min) { return new Date(now - min * 60000).toISOString(); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function delay() { return new Promise(function (r) { setTimeout(r, 200 + Math.random() * 250); }); }
  function fail(message, status, code) { return new A.api.ApiError(message, { status: status, code: code }); }

  var me = { id: "usr_sm", name: "S. Menon", initials: "SM", role: "manager" };

  var owners = {
    rao: { id: "usr_mr", name: "M. Rao" },
    iyer: { id: "usr_ai", name: "A. Iyer" },
    menon: { id: "usr_sm", name: "S. Menon" }
  };

  var campaigns = [
    {
      id: "cmp_us_saas_cto", name: "US SaaS · CTO Outreach", label: "Campaign A",
      icp_summary: "SaaS, Series B–D, CTO / VP Eng, 50–500 FTE",
      status: "live", owner: owners.rao,
      channels: [
        { type: "linkedin", state: "active" }, { type: "email", state: "active" }, { type: "voice", state: "active" }
      ],
      metrics: { prospects: 1284, outreach: 426, meetings: 18 },
      pending_approvals: 3,
      created_at: "2026-08-12T09:00:00Z", updated_at: ago(120), paused_at: null, paused_by: null
    },
    {
      id: "cmp_india_bfsi_cio", name: "India BFSI · CIO Outreach", label: "Campaign B",
      icp_summary: "BFSI, CIO / Head of IT",
      status: "paused", owner: owners.iyer,
      channels: [{ type: "email", state: "active" }, { type: "linkedin", state: "active" }],
      metrics: { prospects: 642, outreach: 211, meetings: 11 },
      pending_approvals: 1,
      created_at: "2026-08-20T09:00:00Z", updated_at: ago(190), paused_at: ago(190), paused_by: owners.iyer
    },
    {
      id: "cmp_voice_ai_founders", name: "Voice AI Founders", label: "Campaign C",
      icp_summary: "AI founders, Seed–Series B",
      status: "live", owner: owners.rao,
      channels: [
        { type: "email", state: "active" }, { type: "voice", state: "active" }, { type: "sms", state: "paused" }
      ],
      metrics: { prospects: 389, outreach: 142, meetings: 9 },
      pending_approvals: 3,
      created_at: "2026-09-01T09:00:00Z", updated_at: ago(45), paused_at: null, paused_by: null
    },
    {
      id: "cmp_enterprise_expansion", name: "Enterprise Expansion", label: "Campaign D",
      icp_summary: "Existing customers",
      status: "draft", owner: owners.menon,
      channels: [],
      metrics: { prospects: 0, outreach: 0, meetings: 0 },
      pending_approvals: 0,
      created_at: "2026-09-14T09:00:00Z", updated_at: ago(1500), paused_at: null, paused_by: null
    }
  ];

  var approvals = [
    { id: "apr_1", campaign_id: "cmp_voice_ai_founders", campaign_name: "Voice AI Founders",
      title: "Voice SDR Agent flagged a pricing objection", reason: "Needs human reply", created_at: ago(2) },
    { id: "apr_2", campaign_id: "cmp_us_saas_cto", campaign_name: "US SaaS · CTO Outreach",
      title: "Outreach Strategy Agent wants to contact a VP-level exec", reason: "Above seniority threshold", created_at: ago(14) },
    { id: "apr_3", campaign_id: "cmp_us_saas_cto", campaign_name: "US SaaS · CTO Outreach",
      title: "Personalisation Agent's draft scored low confidence", reason: "Low confidence", created_at: ago(26) },
    { id: "apr_4", campaign_id: "cmp_india_bfsi_cio", campaign_name: "India BFSI · CIO Outreach",
      title: "A prospect here is also in US SaaS · CTO Outreach", reason: "Conflict", created_at: ago(41) }
  ];
  var approvalsTotal = 7;

  var events = [
    { id: "evt_1", type: "info", campaign_name: "India BFSI · CIO Outreach", message: "paused by A. Iyer. All autonomous execution stopped.", created_at: ago(190) },
    { id: "evt_2", type: "success", campaign_name: "US SaaS · CTO Outreach", message: "prompt v6 activated for the Personalisation Agent", created_at: ago(200) },
    { id: "evt_3", type: "error", campaign_name: "Voice AI Founders", message: "email provider rate limit hit, retried automatically", created_at: ago(840) },
    { id: "evt_4", type: "warning", campaign_name: "US SaaS · CTO Outreach", message: "3 prospects also in India BFSI · CIO Outreach, single-touch owner assigned", created_at: ago(1000) }
  ];

  var killSwitch = { engaged: false, engaged_at: null, engaged_by: null };

  function summary() {
    var live = campaigns.filter(function (c) { return c.status === "live"; }).length;
    return {
      live_campaigns: live,
      total_campaigns: campaigns.filter(function (c) { return c.status !== "archived"; }).length,
      prospects_in_motion: 2315,
      prospects_in_motion_delta_today: 184,
      meetings_30d: 38,
      meetings_30d_delta_wow: 6,
      pending_approvals: approvalsTotal
    };
  }

  function find(id) {
    var c = campaigns.filter(function (x) { return x.id === id; })[0];
    if (!c) throw fail("Campaign not found.", 404, "not_found");
    return c;
  }

  function transition(c, action) {
    var t = new Date().toISOString();
    if (action === "pause") {
      if (c.status !== "live") throw fail("Only a live campaign can be paused.", 409, "invalid_transition");
      c.status = "paused"; c.paused_at = t; c.paused_by = { id: me.id, name: me.name };
      events.unshift({ id: "evt_" + t, type: "info", campaign_name: c.name, message: "paused by " + me.name + ". All autonomous execution stopped.", created_at: t });
    } else if (action === "resume") {
      if (c.status !== "paused") throw fail("Only a paused campaign can be resumed.", 409, "invalid_transition");
      c.status = "live"; c.paused_at = null; c.paused_by = null;
      events.unshift({ id: "evt_" + t, type: "success", campaign_name: c.name, message: "resumed by " + me.name, created_at: t });
    } else if (action === "complete") {
      if (c.status !== "live" && c.status !== "paused") throw fail("Only a live or paused campaign can be completed.", 409, "invalid_transition");
      c.status = "completed"; c.paused_at = null; c.paused_by = null;
    } else if (action === "archive") {
      if (c.status === "archived") throw fail("This campaign is already archived.", 409, "invalid_transition");
      c.status = "archived"; c.paused_at = null; c.paused_by = null;
    }
    c.updated_at = t;
    return c;
  }

  async function handle(method, url, body) {
    await delay();
    var parts = url.split("?");
    var path = parts[0];
    var q = new URLSearchParams(parts[1] || "");
    var m;

    if (method === "GET" && path === "/me") return clone(me);
    if (method === "GET" && path === "/campaigns/summary") return summary();
    if (method === "GET" && path === "/campaigns") return { items: clone(campaigns) };

    if (method === "POST" && (m = path.match(/^\/campaigns\/([^/]+)\/(pause|resume|complete|archive|duplicate)$/))) {
      var c = find(decodeURIComponent(m[1]));
      if (m[2] === "duplicate") {
        var copy = clone(c);
        var t = new Date().toISOString();
        copy.id = "cmp_copy_" + Date.now(); copy.name = c.name + " (copy)"; copy.label = "Copy";
        copy.status = "draft"; copy.metrics = { prospects: 0, outreach: 0, meetings: 0 };
        copy.pending_approvals = 0; copy.created_at = t; copy.updated_at = t;
        copy.paused_at = null; copy.paused_by = null; copy.owner = { id: me.id, name: me.name };
        campaigns.push(copy);
        return clone(copy);
      }
      return clone(transition(c, m[2]));
    }

    if (method === "GET" && path === "/approvals") {
      var limit = parseInt(q.get("limit") || "5", 10);
      return { items: clone(approvals.slice(0, limit)), total: approvalsTotal };
    }
    if (method === "GET" && path === "/events") {
      return { items: clone(events.slice(0, parseInt(q.get("limit") || "5", 10))) };
    }

    if (method === "GET" && path === "/platform/kill-switch") return clone(killSwitch);
    if (method === "PUT" && path === "/platform/kill-switch") {
      killSwitch = body && body.engaged
        ? { engaged: true, engaged_at: new Date().toISOString(), engaged_by: { id: me.id, name: me.name } }
        : { engaged: false, engaged_at: null, engaged_by: null };
      return clone(killSwitch);
    }

    throw fail("No mock for " + method + " " + path, 404, "not_found");
  }

  A.mock = { handle: handle };
})(window.Atlas);
