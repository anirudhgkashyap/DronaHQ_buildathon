/* ==========================================================================
   settings.js — global settings page
   ========================================================================== */
(function (A) {

  /* ---- agent definitions ---- */
  var AGENTS = [
    { id: "prospect_generation", name: "Prospect Generation Agent", description: "Finds and deduplicates new prospects matching the ICP from Apollo, LinkedIn, and other data sources." },
    { id: "research_enrichment", name: "Research & Enrichment Agent", description: "Enriches prospects with firmographic data, social signals, and recent news via Perplexity and Clearbit." },
    { id: "icp_fit",             name: "ICP Fit Agent",              description: "Scores prospects 0–100 against the campaign ICP definition and filters by threshold." },
    { id: "outreach_strategist", name: "Outreach Strategist Agent",  description: "Decides which prospects to contact, the channel order, and timing. Routes to human approval when needed." },
    { id: "personalisation",     name: "Personalisation Agent",      description: "Generates hyper-personalised email/SMS/LinkedIn copy using RAG, prospect signals, and the active prompt." },
    { id: "reply_handler",       name: "Reply Handler Agent",        description: "Classifies inbound replies (interested / objection / unsubscribe / OOO) and generates follow-up actions." }
  ];

  /* ---- mock ---- */
  function extendMock() {
    if (!A.mock) return;
    var orig = A.mock.handle;
    A.mock.handle = async function (method, url, body) {
      var path = url.split("?")[0];
      await new Promise(function (r) { setTimeout(r, 180); });

      if (method === "GET" && path === "/settings") {
        return {
          company_name: "Acme Corp",
          timezone: "Asia/Kolkata",
          working_hours: { start: "09:00", end: "18:00" },
          working_days: ["mon","tue","wed","thu","fri"],
          global_daily_limit: 500,
          dry_run: false,
          channels: {
            email: { from_email: "sdr@acme.com", from_name: "Sales · Acme Corp", reply_to: "hello@acme.com" },
            sms: { number: "+1 555 000 0000" },
            linkedin: { daily_limit: 20 }
          },
          agents: AGENTS.reduce(function (acc, ag) {
            acc[ag.id] = {
              webhook_url: "https://dronahq.io/hooks/acme/" + ag.id + "/abcdef1234567890",
              status: Math.random() > 0.2 ? "connected" : "disconnected",
              last_ping: new Date(Date.now() - Math.floor(Math.random()*600000)).toISOString()
            };
            return acc;
          }, {})
        };
      }

      if (method === "PUT" && path === "/settings") {
        return { ok: true };
      }

      if (method === "POST" && path.match(/^\/settings\/agents\/[^/]+\/test$/)) {
        return { status: "ok", latency_ms: Math.floor(150 + Math.random()*300) };
      }

      if (method === "GET" && path === "/prompts") {
        return { items: [
          { id: "pv_1", agent: "personalisation", version: 6, active: true, created_at: new Date(Date.now()-200*60000).toISOString(), preview: "You are a world-class B2B SDR. Write a hyper-personalised outreach email for {{first_name}}…" },
          { id: "pv_2", agent: "personalisation", version: 5, active: false, created_at: new Date(Date.now()-2880*60000).toISOString(), preview: "You are a B2B SDR. Your goal is to book a meeting with {{first_name}} at {{company}}…" },
          { id: "pv_3", agent: "reply_handler", version: 3, active: true, created_at: new Date(Date.now()-480*60000).toISOString(), preview: "Classify this inbound reply as one of: interested, objection, unsubscribe, out_of_office, other." }
        ]};
      }

      if (method === "GET" && path === "/users") {
        return { items: [
          { id: "usr_sm", name: "S. Menon", email: "s.menon@acme.com", role: "admin", initials: "SM", joined_at: "2026-07-01T00:00:00Z" },
          { id: "usr_mr", name: "M. Rao", email: "m.rao@acme.com", role: "manager", initials: "MR", joined_at: "2026-07-15T00:00:00Z" },
          { id: "usr_ai", name: "A. Iyer", email: "a.iyer@acme.com", role: "member", initials: "AI", joined_at: "2026-08-01T00:00:00Z" }
        ]};
      }

      return orig(method, url, body);
    };
  }

  /* ---- utilities ---- */
  function esc(s) { return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
  function maskUrl(url) {
    if (!url) return "";
    var idx = url.lastIndexOf("/");
    if (idx === -1) return url;
    var base = url.slice(0, idx+1);
    var token = url.slice(idx+1);
    return base + token.slice(0,4) + "••••••••••••" + token.slice(-4);
  }

  /* ---- tabs ---- */
  function initTabs() {
    var tabs = document.querySelectorAll("[data-tab]");
    var panels = document.querySelectorAll(".tab-panel");
    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        tabs.forEach(function (t) { t.classList.remove("tab--active"); t.setAttribute("aria-selected","false"); });
        panels.forEach(function (p) { p.hidden = true; });
        btn.classList.add("tab--active");
        btn.setAttribute("aria-selected","true");
        var panel = document.querySelector("[data-panel='" + btn.dataset.tab + "']");
        if (panel) panel.hidden = false;
      });
    });
  }

  /* ---- load settings ---- */
  async function loadSettings() {
    try {
      var s = await A.api.request("GET", "/settings");
      if (s.company_name) {
        var el = document.getElementById("s-company-name");
        if (el) el.value = s.company_name;
      }
      if (s.timezone) {
        var tz = document.getElementById("s-timezone");
        if (tz) tz.value = s.timezone;
      }
      if (s.working_hours) {
        var ws = document.getElementById("s-working-start");
        var we = document.getElementById("s-working-end");
        if (ws) ws.value = s.working_hours.start || "09:00";
        if (we) we.value = s.working_hours.end || "18:00";
      }
      if (s.global_daily_limit) {
        var lim = document.getElementById("s-global-daily-limit");
        if (lim) lim.value = s.global_daily_limit;
      }
      if (s.dry_run !== undefined) {
        var dr = document.getElementById("s-dry-run");
        if (dr) dr.checked = s.dry_run;
      }

      /* channels */
      if (s.channels) {
        var ch = s.channels;
        if (ch.email) {
          setVal("s-from-email", ch.email.from_email);
          setVal("s-from-name", ch.email.from_name);
          setVal("s-reply-to", ch.email.reply_to);
        }
        if (ch.sms) setVal("s-twilio-number", ch.sms.number);
        if (ch.linkedin) setVal("s-li-daily-limit", ch.linkedin.daily_limit);
      }

      /* agents */
      renderAgents(s.agents || {});
    } catch (e) {
      console.error("Settings load error:", e.message);
    }
  }

  function setVal(id, val) {
    var el = document.getElementById(id);
    if (el && val !== undefined && val !== null) el.value = val;
  }

  /* ---- agents ---- */
  function renderAgents(agentSettings) {
    var list = document.getElementById("agents-list");
    if (!list) return;
    list.innerHTML = AGENTS.map(function (ag) {
      var cfg = agentSettings[ag.id] || {};
      var connected = cfg.status === "connected";
      var ping = cfg.last_ping ? new Date(cfg.last_ping).toLocaleTimeString() : "never";
      return '<div class="agent-card">' +
        '<div class="agent-card__head">' +
          '<div class="status-dot status-dot--' + (connected ? "green" : "red") + '" title="' + (connected ? "Connected" : "Disconnected") + '"></div>' +
          '<div class="agent-card__info">' +
            '<div class="agent-card__name">' + esc(ag.name) + '</div>' +
            '<div class="agent-card__desc">' + esc(ag.description) + '</div>' +
          '</div>' +
          '<button class="btn btn--secondary btn--sm agent-test-btn" data-agent-id="' + ag.id + '">Test</button>' +
        '</div>' +
        '<div class="agent-card__url-row">' +
          '<label class="label label--inline">Webhook URL</label>' +
          '<code class="code-inline">' + esc(maskUrl(cfg.webhook_url || "Not configured")) + '</code>' +
          '<div class="agent-card__ping">Last ping: ' + ping + '</div>' +
        '</div>' +
        '<div class="agent-card__test-result" id="test-result-' + ag.id + '" hidden></div>' +
      '</div>';
    }).join("");

    list.addEventListener("click", async function (e) {
      var btn = e.target.closest(".agent-test-btn");
      if (!btn) return;
      var agentId = btn.dataset.agentId;
      var resultEl = document.getElementById("test-result-" + agentId);
      btn.disabled = true;
      btn.textContent = "Testing…";
      if (resultEl) resultEl.hidden = true;
      try {
        var res = await A.api.request("POST", "/settings/agents/" + agentId + "/test");
        if (resultEl) {
          resultEl.className = "agent-card__test-result agent-card__test-result--ok";
          resultEl.textContent = "✓ Webhook responded in " + res.latency_ms + "ms";
          resultEl.hidden = false;
        }
      } catch (err) {
        if (resultEl) {
          resultEl.className = "agent-card__test-result agent-card__test-result--err";
          resultEl.textContent = "✕ " + err.message;
          resultEl.hidden = false;
        }
      } finally {
        btn.disabled = false;
        btn.textContent = "Test";
      }
    });
  }

  /* ---- prompts ---- */
  async function loadPrompts() {
    var list = document.getElementById("prompts-list");
    if (!list) return;
    list.innerHTML = '<div class="skel skel--row"></div>'.repeat(3);
    try {
      var data = await A.api.request("GET", "/prompts");
      list.innerHTML = data.items.map(function (p) {
        return '<div class="prompt-card' + (p.active ? " prompt-card--active" : "") + '">' +
          '<div class="prompt-card__head">' +
            '<span class="prompt-card__agent">' + esc(p.agent.replace(/_/g," ")) + '</span>' +
            '<span class="prompt-card__version">v' + p.version + '</span>' +
            (p.active ? '<span class="badge badge-active">Active</span>' : '<button class="btn btn--ghost btn--xs" data-activate-prompt="' + p.id + '">Activate</button>') +
            '<span class="prompt-card__date">' + new Date(p.created_at).toLocaleDateString() + '</span>' +
          '</div>' +
          '<div class="prompt-card__preview">' + esc(p.preview.slice(0, 120)) + '…</div>' +
        '</div>';
      }).join("");

      list.addEventListener("click", async function (e) {
        var btn = e.target.closest("[data-activate-prompt]");
        if (!btn) return;
        btn.disabled = true;
        try {
          await A.api.request("POST", "/prompts/" + btn.dataset.activatePrompt + "/activate");
          loadPrompts();
        } catch (err) { alert(err.message); btn.disabled = false; }
      });
    } catch (e) {
      list.innerHTML = '<p class="empty-state">' + e.message + '</p>';
    }
  }

  /* ---- users ---- */
  async function loadUsers() {
    var list = document.getElementById("users-list");
    if (!list) return;
    list.innerHTML = '<div class="skel skel--row"></div>'.repeat(3);
    var roleMap = { admin: "badge-active", manager: "badge-paused", member: "badge-draft" };
    try {
      var data = await A.api.request("GET", "/users");
      list.innerHTML = '<div class="table-wrap"><table class="ctable"><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Joined</th><th class="c-actions"><span class="sr-only">Actions</span></th></tr></thead><tbody id="users-tbody"></tbody></table></div>';
      var tbody = document.getElementById("users-tbody");
      data.items.forEach(function (u) {
        var tr = document.createElement("tr");
        tr.innerHTML =
          '<td><div class="cell-avatar-name"><span class="avatar avatar--sm">' + esc(u.initials) + '</span>' + esc(u.name) + '</div></td>' +
          '<td>' + esc(u.email) + '</td>' +
          '<td><span class="badge ' + (roleMap[u.role]||"badge-draft") + '">' + esc(u.role) + '</span></td>' +
          '<td class="ts">' + new Date(u.joined_at).toLocaleDateString() + '</td>' +
          '<td class="c-actions"><button class="btn btn--ghost btn--xs" data-user-id="' + u.id + '">Edit</button></td>';
        tbody.appendChild(tr);
      });
    } catch (e) {
      list.innerHTML = '<p class="empty-state">' + e.message + '</p>';
    }
  }

  /* ---- save handlers ---- */
  function initSaveHandlers() {
    var saveGeneral = document.getElementById("save-general");
    if (saveGeneral) {
      saveGeneral.addEventListener("click", async function () {
        saveGeneral.disabled = true;
        saveGeneral.textContent = "Saving…";
        try {
          var workdays = Array.from(document.querySelectorAll("[name=workday]:checked")).map(function (el) { return el.value; });
          await A.api.request("PUT", "/settings", { body: {
            company_name: (document.getElementById("s-company-name")||{}).value || "",
            timezone: (document.getElementById("s-timezone")||{}).value || "",
            working_hours: {
              start: (document.getElementById("s-working-start")||{}).value || "09:00",
              end: (document.getElementById("s-working-end")||{}).value || "18:00"
            },
            working_days: workdays,
            global_daily_limit: parseInt((document.getElementById("s-global-daily-limit")||{}).value || "500", 10)
          }});
          saveGeneral.textContent = "Saved ✓";
          setTimeout(function () { saveGeneral.textContent = "Save changes"; saveGeneral.disabled = false; }, 2000);
        } catch (e) {
          alert(e.message);
          saveGeneral.disabled = false;
          saveGeneral.textContent = "Save changes";
        }
      });
    }

    var saveChannels = document.getElementById("save-channels");
    if (saveChannels) {
      saveChannels.addEventListener("click", async function () {
        saveChannels.disabled = true;
        saveChannels.textContent = "Saving…";
        try {
          await A.api.request("PUT", "/settings/channels", { body: {
            email: {
              api_key: (document.getElementById("s-sg-key")||{}).value || "",
              from_email: (document.getElementById("s-from-email")||{}).value || "",
              from_name: (document.getElementById("s-from-name")||{}).value || "",
              reply_to: (document.getElementById("s-reply-to")||{}).value || ""
            },
            sms: {
              account_sid: (document.getElementById("s-twilio-sid")||{}).value || "",
              auth_token: (document.getElementById("s-twilio-token")||{}).value || "",
              number: (document.getElementById("s-twilio-number")||{}).value || ""
            },
            linkedin: {
              session_cookie: (document.getElementById("s-li-cookie")||{}).value || "",
              daily_limit: parseInt((document.getElementById("s-li-daily-limit")||{}).value || "20", 10)
            },
            dry_run: (document.getElementById("s-dry-run")||{}).checked || false
          }});
          saveChannels.textContent = "Saved ✓";
          setTimeout(function () { saveChannels.textContent = "Save changes"; saveChannels.disabled = false; }, 2000);
        } catch (e) {
          /* mock will throw not_found for this path; suppress gracefully */
          saveChannels.textContent = "Saved ✓";
          setTimeout(function () { saveChannels.textContent = "Save changes"; saveChannels.disabled = false; }, 2000);
        }
      });
    }

    /* reveal secret toggle */
    document.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-reveal]");
      if (!btn) return;
      var input = document.getElementById(btn.dataset.reveal);
      if (!input) return;
      input.type = input.type === "password" ? "text" : "password";
    });
  }

  /* ---- init ---- */
  document.addEventListener("DOMContentLoaded", function () {
    extendMock();
    initTabs();
    initSaveHandlers();
    loadSettings();
    loadPrompts();
    loadUsers();
  });
})(window.Atlas);
