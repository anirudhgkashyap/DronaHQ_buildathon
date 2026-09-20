/* ==========================================================================
   campaign-detail.js — single campaign view
   ========================================================================== */
(function (A) {
  var campaignId = new URLSearchParams(location.search).get("id");

  function extendMock() {
    if (!A.mock) return;
    var origHandle = A.mock.handle;
    A.mock.handle = async function (method, url, body) {
      var parts = url.split("?"), path = parts[0];
      var m;

      if (method === "GET" && (m = path.match(/^\/campaigns\/([^/]+)$/))) {
        await new Promise(function (r) { setTimeout(r, 200); });
        var id = decodeURIComponent(m[1]);
        return {
          id: id, name: "US SaaS · CTO Outreach",
          icp_summary: "SaaS, Series B–D, CTO / VP Eng, 50–500 FTE",
          status: "live", owner: { id: "usr_mr", name: "M. Rao" },
          channels: [{ type: "linkedin", state: "active" }, { type: "email", state: "active" }],
          metrics: { prospects: 1284, contacted: 426, replies: 87, meetings: 18, open_rate: 0.42, reply_rate: 0.204 },
          pending_approvals: 3, goal: "book_meetings", daily_limit: 80,
          timezone: "America/New_York", created_at: "2026-08-12T09:00:00Z"
        };
      }

      if (method === "GET" && (m = path.match(/^\/campaigns\/([^/]+)\/prospects$/))) {
        await new Promise(function (r) { setTimeout(r, 220); });
        return { items: [
          { id: "p1", name: "Alice Chen", company: "Stripe", title: "CTO", stage: "replied", score: 92, last_activity: new Date(Date.now()-2*60000).toISOString() },
          { id: "p2", name: "Bob Martinez", company: "Figma", title: "VP Engineering", stage: "contacted", score: 78, last_activity: new Date(Date.now()-45*60000).toISOString() },
          { id: "p3", name: "Carol Kim", company: "Notion", title: "CTO", stage: "meeting_booked", score: 95, last_activity: new Date(Date.now()-120*60000).toISOString() },
          { id: "p4", name: "David Singh", company: "Linear", title: "Head of Engineering", stage: "enrolled", score: 65, last_activity: new Date(Date.now()-300*60000).toISOString() },
          { id: "p5", name: "Eva Patel", company: "Vercel", title: "CTO", stage: "new", score: 81, last_activity: new Date(Date.now()-1440*60000).toISOString() },
          { id: "p6", name: "Frank Lee", company: "PlanetScale", title: "VP Engineering", stage: "unsubscribed", score: 55, last_activity: new Date(Date.now()-3000*60000).toISOString() }
        ]};
      }

      if (method === "GET" && (m = path.match(/^\/campaigns\/([^/]+)\/approvals$/))) {
        await new Promise(function (r) { setTimeout(r, 200); });
        return { items: [
          { id: "apr_1", title: "Voice SDR Agent flagged a pricing objection", reason: "Needs human reply", prospect_name: "Alice Chen", prospect_company: "Stripe", created_at: new Date(Date.now()-2*60000).toISOString() },
          { id: "apr_2", title: "Outreach Strategy Agent wants to contact a VP-level exec", reason: "Above seniority threshold", prospect_name: "Bob Martinez", prospect_company: "Figma", created_at: new Date(Date.now()-14*60000).toISOString() },
          { id: "apr_3", title: "Personalisation Agent's draft scored low confidence", reason: "Low confidence", prospect_name: "Eva Patel", prospect_company: "Vercel", created_at: new Date(Date.now()-26*60000).toISOString() }
        ]};
      }

      if (method === "POST" && (m = path.match(/^\/approvals\/([^/]+)\/(approve|reject)$/))) {
        await new Promise(function (r) { setTimeout(r, 250); });
        return { ok: true };
      }

      if (method === "GET" && (m = path.match(/^\/campaigns\/([^/]+)\/agent-runs$/))) {
        await new Promise(function (r) { setTimeout(r, 200); });
        return { items: [
          { id: "run_1", agent: "Personalisation Agent", status: "success", summary: "Generated 12 personalised email drafts. Avg confidence 0.89.", started_at: new Date(Date.now()-5*60000).toISOString(), duration_ms: 4300 },
          { id: "run_2", agent: "ICP Fit Agent", status: "success", summary: "Scored 34 new prospects. 18 passed ICP threshold (≥70).", started_at: new Date(Date.now()-25*60000).toISOString(), duration_ms: 7100 },
          { id: "run_3", agent: "Reply Handler Agent", status: "human_required", summary: "Detected pricing objection from Alice Chen. Stopped for human review.", started_at: new Date(Date.now()-45*60000).toISOString(), duration_ms: 1200 },
          { id: "run_4", agent: "Outreach Strategist Agent", status: "error", summary: "Email provider rate limit hit. Will retry in 15 min.", started_at: new Date(Date.now()-120*60000).toISOString(), duration_ms: 890 },
          { id: "run_5", agent: "Prospect Generation Agent", status: "success", summary: "Fetched 42 new prospects from Apollo. 30 deduplicated into pool.", started_at: new Date(Date.now()-240*60000).toISOString(), duration_ms: 9400 }
        ]};
      }

      return origHandle(method, url, body);
    };
  }

  function el(id) { return document.getElementById(id); }
  function reltime(iso) {
    var d = new Date(iso), diff = Math.floor((Date.now() - d) / 1000);
    if (diff < 60) return diff + "s ago";
    if (diff < 3600) return Math.floor(diff/60) + "m ago";
    if (diff < 86400) return Math.floor(diff/3600) + "h ago";
    return Math.floor(diff/86400) + "d ago";
  }
  function pct(n) { return (n * 100).toFixed(1) + "%"; }
  function statusBadge(s) {
    var map = { live: ["badge-active","Live"], paused: ["badge-paused","Paused"], draft: ["badge-draft","Draft"], completed: ["badge-draft","Completed"], archived: ["badge-draft","Archived"] };
    var r = map[s] || ["badge-draft", s];
    return '<span class="badge ' + r[0] + '">' + r[1] + '</span>';
  }
  function stageBadge(s) {
    var map = { new: "badge-draft", enrolled: "badge-draft", contacted: "badge-paused", replied: "badge-active", meeting_booked: "badge-active", unsubscribed: "badge-draft" };
    var label = s.replace(/_/g," ");
    return '<span class="badge ' + (map[s]||"badge-draft") + '">' + label + '</span>';
  }
  function esc(s) { return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
  function scoreClass(s) { return s >= 80 ? "high" : s >= 60 ? "mid" : "low"; }

  function initTabs() {
    var tabs = document.querySelectorAll("[data-tab]");
    var panels = document.querySelectorAll(".tab-panel");
    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        tabs.forEach(function (t) { t.classList.remove("tab--active"); t.setAttribute("aria-selected","false"); });
        panels.forEach(function (p) { p.hidden = true; });
        btn.classList.add("tab--active"); btn.setAttribute("aria-selected","true");
        var panel = document.querySelector("[data-panel='" + btn.dataset.tab + "']");
        if (panel) panel.hidden = false;
      });
    });
  }

  async function loadCampaign() {
    try {
      var c = await A.api.request("GET", "/campaigns/" + encodeURIComponent(campaignId));
      document.title = c.name + " · Atlas SDR";
      el("breadcrumb-name").textContent = c.name;
      el("cd-name").textContent = c.name; el("cd-name").classList.remove("skel","skel--text","skel--wide");
      el("cd-icp").textContent = c.icp_summary || ""; el("cd-icp").classList.remove("skel","skel--text");
      el("cd-badge").innerHTML = statusBadge(c.status);
      var acts = '<a class="btn btn--secondary btn--sm" href="campaign-new.html?id=' + encodeURIComponent(c.id) + '"><svg class="icon"><use href="#i-edit"/></svg>Edit</a>';
      if (c.status === "live") acts += '<button class="btn btn--secondary btn--sm" id="cd-pause-btn"><svg class="icon"><use href="#i-pause"/></svg>Pause</button>';
      if (c.status === "paused") acts += '<button class="btn btn--secondary btn--sm" id="cd-resume-btn"><svg class="icon"><use href="#i-play"/></svg>Resume</button>';
      acts += '<button class="btn btn--ghost btn--sm" id="cd-archive-btn"><svg class="icon"><use href="#i-archive"/></svg>Archive</button>';
      el("cd-actions").innerHTML = acts;
      ["pause","resume","archive"].forEach(function (action) {
        var btn = el("cd-" + action + "-btn");
        if (!btn) return;
        btn.addEventListener("click", async function () {
          btn.disabled = true;
          try { await A.api.campaignAction(c.id, action); location.reload(); }
          catch (e) { alert(e.message); btn.disabled = false; }
        });
      });
      var m = c.metrics;
      el("m-prospects").textContent = (m.prospects || 0).toLocaleString();
      el("m-contacted").textContent = (m.contacted || 0).toLocaleString();
      el("m-replies").textContent = (m.replies || 0).toLocaleString();
      el("m-meetings").textContent = (m.meetings || 0).toLocaleString();
      el("m-open-rate").textContent = pct(m.open_rate || 0);
      el("m-reply-rate").textContent = pct(m.reply_rate || 0);
      ["m-prospects","m-contacted","m-replies","m-meetings","m-open-rate","m-reply-rate"].forEach(function (id) { el(id).classList.remove("skel","skel--text"); });
      if (c.pending_approvals) { var badge = el("approvals-tab-count"); badge.textContent = c.pending_approvals; badge.hidden = false; }
    } catch (e) { el("campaign-hd").innerHTML = '<p class="empty-state">' + e.message + '</p>'; }
  }

  async function loadProspects() {
    var tbody = el("prospect-rows");
    tbody.innerHTML = '<tr><td colspan="6"><div class="skel skel--row"></div></td></tr>'.repeat(5);
    try {
      var data = await A.api.request("GET", "/campaigns/" + encodeURIComponent(campaignId) + "/prospects");
      tbody.innerHTML = "";
      data.items.forEach(function (p) {
        var tr = document.createElement("tr");
        tr.innerHTML = '<td><div class="cell-name">' + esc(p.name) + '</div><div class="cell-sub">' + esc(p.title||"") + '</div></td>' +
          '<td>' + esc(p.company) + '</td>' + '<td>' + stageBadge(p.stage) + '</td>' +
          '<td class="num"><span class="score score--' + scoreClass(p.score) + '">' + p.score + '</span></td>' +
          '<td class="ts">' + reltime(p.last_activity) + '</td>' +
          '<td class="c-actions"><a class="btn btn--ghost btn--xs" href="conversations.html?prospect=' + encodeURIComponent(p.id) + '">Outreach</a></td>';
        tbody.appendChild(tr);
      });
      if (!data.items.length) el("prospect-state").innerHTML = '<p class="empty-state">No prospects yet.</p>';
    } catch (e) { el("prospect-state").innerHTML = '<p class="empty-state">' + e.message + '</p>'; }
  }

  async function loadApprovals() {
    var list = el("cd-approvals-list");
    list.innerHTML = '<li class="skel skel--row"></li>'.repeat(3);
    try {
      var data = await A.api.request("GET", "/campaigns/" + encodeURIComponent(campaignId) + "/approvals");
      list.innerHTML = "";
      data.items.forEach(function (ap) {
        var li = document.createElement("li");
        li.className = "approval-card";
        li.innerHTML = '<div class="approval-card__body"><div class="approval-card__title">' + esc(ap.title) + '</div>' +
          '<div class="approval-card__meta">' + esc(ap.prospect_name) + ' · ' + esc(ap.prospect_company) + ' · ' + esc(ap.reason) + ' · ' + reltime(ap.created_at) + '</div></div>' +
          '<div class="approval-card__actions"><button class="btn btn--primary btn--sm" data-apr-id="' + ap.id + '" data-action="approve"><svg class="icon"><use href="#i-check"/></svg>Approve</button>' +
          '<button class="btn btn--danger btn--sm" data-apr-id="' + ap.id + '" data-action="reject"><svg class="icon"><use href="#i-x"/></svg>Reject</button></div>';
        list.appendChild(li);
      });
      if (!data.items.length) el("cd-approvals-state").innerHTML = '<p class="empty-state">No pending approvals.</p>';
      list.addEventListener("click", async function (e) {
        var btn = e.target.closest("[data-apr-id]");
        if (!btn) return;
        btn.disabled = true;
        var sibling = btn.parentElement.querySelector("[data-apr-id]:not(:disabled)");
        if (sibling) sibling.disabled = true;
        try { await A.api.request("POST", "/approvals/" + btn.dataset.aprId + "/" + btn.dataset.action); btn.closest("li").remove(); }
        catch (err) { alert(err.message); btn.disabled = false; if (sibling) sibling.disabled = false; }
      });
    } catch (e) { el("cd-approvals-state").innerHTML = '<p class="empty-state">' + e.message + '</p>'; }
  }

  async function loadAgentRuns() {
    var list = el("runs-list");
    list.innerHTML = '<li class="skel skel--row"></li>'.repeat(4);
    try {
      var data = await A.api.request("GET", "/campaigns/" + encodeURIComponent(campaignId) + "/agent-runs");
      list.innerHTML = "";
      var statusIcon = { success: "✓", error: "✕", human_required: "⚑", running: "↻" };
      var statusClass = { success: "run--success", error: "run--error", human_required: "run--warn", running: "run--running" };
      data.items.forEach(function (r) {
        var li = document.createElement("li");
        li.className = "run-item " + (statusClass[r.status]||"");
        li.innerHTML = '<div class="run-item__icon" aria-hidden="true">' + (statusIcon[r.status]||"·") + '</div>' +
          '<div class="run-item__body"><div class="run-item__agent">' + esc(r.agent) + '</div>' +
          '<div class="run-item__summary">' + esc(r.summary) + '</div>' +
          '<div class="run-item__meta">' + reltime(r.started_at) + ' · ' + (r.duration_ms/1000).toFixed(1) + 's</div></div>';
        list.appendChild(li);
      });
      if (!data.items.length) el("runs-state").innerHTML = '<p class="empty-state">No agent runs yet.</p>';
    } catch (e) { el("runs-state").innerHTML = '<p class="empty-state">' + e.message + '</p>'; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!campaignId) { document.getElementById("content").innerHTML = '<p class="empty-state">No campaign ID specified.</p>'; return; }
    extendMock(); initTabs(); loadCampaign(); loadProspects(); loadApprovals(); loadAgentRuns();
  });
})(window.Atlas);
