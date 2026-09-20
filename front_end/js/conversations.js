/* ==========================================================================
   conversations.js — two-pane conversation inbox
   ========================================================================== */
(function (A) {
  var activeConvId = null;
  var activeFilter = "all";
  var conversations = [];

  /* ---- extend mock ---- */
  function extendMock() {
    if (!A.mock) return;
    var orig = A.mock.handle;
    A.mock.handle = async function (method, url, body) {
      var parts = url.split("?"), path = parts[0], q = new URLSearchParams(parts[1]||"");
      var m;
      await new Promise(function (r) { setTimeout(r, 180); });

      if (method === "GET" && path === "/conversations") {
        var filter = q.get("filter") || "all";
        var all = mockConversations();
        var filtered = filter === "all" ? all : all.filter(function (c) {
          if (filter === "replied") return c.status === "replied";
          if (filter === "active") return c.status === "active";
          if (filter === "unread") return c.unread > 0;
          if (filter === "approvals") return c.has_approval;
          return true;
        });
        return { items: filtered };
      }

      if (method === "GET" && (m = path.match(/^\/conversations\/([^/]+)\/messages$/))) {
        return { items: mockMessages(m[1]) };
      }

      if (method === "GET" && (m = path.match(/^\/conversations\/([^/]+)\/ai-suggestions$/))) {
        return { items: [
          "Thanks for your interest! Would you be open to a 20-minute call this week?",
          "I noticed you're expanding the engineering team — our platform could help you move faster. Happy to share a quick demo?",
          "Let me know a good time and I'll send a calendar invite."
        ]};
      }

      if (method === "POST" && (m = path.match(/^\/conversations\/([^/]+)\/messages$/))) {
        return { id: "msg_" + Date.now(), status: "sent" };
      }

      return orig(method, url, body);
    };
  }

  function mockConversations() {
    var now = Date.now();
    return [
      { id: "conv_1", prospect: { id: "p1", name: "Alice Chen", company: "Stripe", email: "alice@stripe.com", icp_score: 92, title: "CTO" }, status: "replied", unread: 2, has_approval: false, preview: "Thanks for reaching out! I'd love to connect…", updated_at: new Date(now - 2*60000).toISOString(), campaign_name: "US SaaS · CTO Outreach" },
      { id: "conv_2", prospect: { id: "p2", name: "Bob Martinez", company: "Figma", email: "bob@figma.com", icp_score: 78, title: "VP Engineering" }, status: "active", unread: 0, has_approval: true, preview: "Saw your email — let me check with the team.", updated_at: new Date(now - 25*60000).toISOString(), campaign_name: "US SaaS · CTO Outreach" },
      { id: "conv_3", prospect: { id: "p3", name: "Carol Kim", company: "Notion", email: "carol@notion.so", icp_score: 95, title: "CTO" }, status: "replied", unread: 0, has_approval: false, preview: "Meeting booked for Tuesday at 10am PT.", updated_at: new Date(now - 90*60000).toISOString(), campaign_name: "US SaaS · CTO Outreach" },
      { id: "conv_4", prospect: { id: "p4", name: "David Singh", company: "Linear", email: "david@linear.app", icp_score: 65, title: "Head of Engineering" }, status: "active", unread: 1, has_approval: false, preview: "Who are you again?", updated_at: new Date(now - 3*3600000).toISOString(), campaign_name: "Voice AI Founders" },
      { id: "conv_5", prospect: { id: "p5", name: "Eva Patel", company: "Vercel", email: "eva@vercel.com", icp_score: 81, title: "CTO" }, status: "active", unread: 0, has_approval: true, preview: "Your email was marked as potential spam…", updated_at: new Date(now - 5*3600000).toISOString(), campaign_name: "Voice AI Founders" },
      { id: "conv_6", prospect: { id: "p6", name: "Frank Lee", company: "PlanetScale", email: "frank@planetscale.com", icp_score: 55, title: "VP Engineering" }, status: "active", unread: 0, has_approval: false, preview: "Not interested at this time.", updated_at: new Date(now - 2*86400000).toISOString(), campaign_name: "India BFSI · CIO Outreach" }
    ];
  }

  function mockMessages(convId) {
    var now = Date.now();
    var map = {
      conv_1: [
        { id: "m1", direction: "outbound", channel: "email", subject: "Quick question for Alice", body: "Hi Alice,\n\nI noticed Stripe is scaling its engineering org rapidly. We help CTOs at Series B–D SaaS companies cut their release cycle by 40%.\n\nWould a 20-minute call next week make sense?\n\nBest,\nS. Menon", sent_at: new Date(now - 3*86400000).toISOString() },
        { id: "m2", direction: "inbound", channel: "email", body: "Thanks for reaching out! I'd love to connect — what does your platform actually do? Can you send more detail?", sent_at: new Date(now - 2*60000).toISOString() }
      ],
      conv_2: [
        { id: "m3", direction: "outbound", channel: "email", subject: "For Bob @ Figma", body: "Hi Bob, wanted to reach out about your engineering velocity goals…", sent_at: new Date(now - 5*86400000).toISOString() },
        { id: "m4", direction: "inbound", channel: "email", body: "Saw your email — let me check with the team.", sent_at: new Date(now - 25*60000).toISOString() },
        { id: "m5", type: "approval", body: "⚑ Approval required: Outreach Strategist wants to follow up immediately. Review before proceeding.", sent_at: new Date(now - 15*60000).toISOString(), approval_id: "apr_2" }
      ]
    };
    return map[convId] || [
      { id: "m_" + convId, direction: "outbound", channel: "email", subject: "Initial outreach", body: "Hi, I wanted to reach out about a potential collaboration…", sent_at: new Date(now - 86400000).toISOString() }
    ];
  }

  /* ---- utilities ---- */
  function esc(s) { return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
  function reltime(iso) {
    var d = new Date(iso), diff = Math.floor((Date.now() - d) / 1000);
    if (diff < 60) return diff + "s";
    if (diff < 3600) return Math.floor(diff/60) + "m";
    if (diff < 86400) return Math.floor(diff/3600) + "h";
    return Math.floor(diff/86400) + "d";
  }
  function initials(name) { return name.split(" ").map(function (w) { return w[0]; }).join("").slice(0,2).toUpperCase(); }
  function scoreClass(s) { return s >= 80 ? "high" : s >= 60 ? "mid" : "low"; }

  /* ---- render conversation list ---- */
  function renderConvList(items) {
    var list = document.getElementById("conv-list");
    list.innerHTML = "";
    if (!items.length) {
      document.getElementById("conv-list-state").innerHTML = '<p class="empty-state">No conversations match this filter.</p>';
      return;
    }
    document.getElementById("conv-list-state").innerHTML = "";
    items.forEach(function (conv) {
      var li = document.createElement("li");
      li.className = "conv-item" + (conv.unread ? " conv-item--unread" : "") + (conv.id === activeConvId ? " conv-item--active" : "");
      li.dataset.convId = conv.id;
      li.setAttribute("role","button");
      li.setAttribute("tabindex","0");
      li.innerHTML =
        '<div class="conv-item__avatar" aria-hidden="true">' + esc(initials(conv.prospect.name)) + '</div>' +
        '<div class="conv-item__body">' +
          '<div class="conv-item__name-row">' +
            '<span class="conv-item__name">' + esc(conv.prospect.name) + '</span>' +
            '<span class="conv-item__time">' + reltime(conv.updated_at) + '</span>' +
          '</div>' +
          '<div class="conv-item__company">' + esc(conv.prospect.company) + ' · ' + esc(conv.campaign_name) + '</div>' +
          '<div class="conv-item__preview">' + esc(conv.preview) + '</div>' +
        '</div>' +
        (conv.unread ? '<span class="conv-item__badge">' + conv.unread + '</span>' : '') +
        (conv.has_approval ? '<span class="conv-item__approval-dot" title="Approval required"></span>' : '');
      li.addEventListener("click", function () { openConversation(conv); });
      li.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openConversation(conv); }});
      list.appendChild(li);
    });
  }

  /* ---- open a conversation ---- */
  async function openConversation(conv) {
    activeConvId = conv.id;

    /* mobile: show thread pane */
    document.getElementById("inbox-list-pane").classList.add("inbox__list--hidden");
    document.getElementById("inbox-back").hidden = false;
    document.getElementById("thread-empty").hidden = true;
    document.getElementById("thread-content").hidden = false;

    /* highlight active */
    document.querySelectorAll(".conv-item").forEach(function (el) {
      el.classList.toggle("conv-item--active", el.dataset.convId === conv.id);
    });

    /* prospect header */
    var p = conv.prospect;
    document.getElementById("thread-header").innerHTML =
      '<div class="prospect-card">' +
        '<div class="prospect-card__avatar" aria-hidden="true">' + esc(initials(p.name)) + '</div>' +
        '<div class="prospect-card__info">' +
          '<div class="prospect-card__name">' + esc(p.name) + '</div>' +
          '<div class="prospect-card__meta">' + esc(p.title) + ' · ' + esc(p.company) + ' · ' + esc(p.email) + '</div>' +
          '<div class="prospect-card__campaign">' + esc(conv.campaign_name) + '</div>' +
        '</div>' +
        '<div class="prospect-card__score">' +
          '<div class="score score--' + scoreClass(p.icp_score) + ' score--lg">' + p.icp_score + '</div>' +
          '<div class="score__label">ICP score</div>' +
        '</div>' +
      '</div>';

    document.getElementById("compose-to").textContent = p.name + " <" + p.email + ">";

    /* load messages */
    var msgEl = document.getElementById("thread-messages");
    msgEl.innerHTML = '<div class="skel skel--row"></div>'.repeat(3);
    try {
      var data = await A.api.request("GET", "/conversations/" + conv.id + "/messages");
      msgEl.innerHTML = "";
      data.items.forEach(function (msg) {
        if (msg.type === "approval") {
          var div = document.createElement("div");
          div.className = "msg-approval";
          div.innerHTML =
            '<svg class="icon" aria-hidden="true"><use href="#i-bot"/></svg>' +
            '<span>' + esc(msg.body) + '</span>' +
            '<div class="msg-approval__actions">' +
              '<button class="btn btn--primary btn--xs" data-apr-id="' + msg.approval_id + '" data-action="approve"><svg class="icon"><use href="#i-check"/></svg>Approve</button>' +
              '<button class="btn btn--danger btn--xs" data-apr-id="' + msg.approval_id + '" data-action="reject"><svg class="icon"><use href="#i-x"/></svg>Reject</button>' +
            '</div>';
          msgEl.appendChild(div);
          return;
        }
        var div = document.createElement("div");
        div.className = "msg msg--" + (msg.direction || "outbound");
        div.innerHTML =
          (msg.subject ? '<div class="msg__subject">' + esc(msg.subject) + '</div>' : '') +
          '<div class="msg__body">' + esc(msg.body).replace(/\n/g,"<br>") + '</div>' +
          '<div class="msg__meta">' + (msg.direction === "inbound" ? "Received" : "Sent") + ' · ' + esc(msg.channel||"email") + ' · ' + new Date(msg.sent_at).toLocaleString() + '</div>';
        msgEl.appendChild(div);
      });
      msgEl.scrollTop = msgEl.scrollHeight;
    } catch (e) {
      msgEl.innerHTML = '<p class="empty-state">' + e.message + '</p>';
    }

    /* approval buttons in thread */
    msgEl.addEventListener("click", async function (e) {
      var btn = e.target.closest("[data-apr-id]");
      if (!btn) return;
      btn.disabled = true;
      try {
        await A.api.request("POST", "/approvals/" + btn.dataset.aprId + "/" + btn.dataset.action);
        btn.closest(".msg-approval").innerHTML = '<span>✓ ' + btn.dataset.action + 'd</span>';
      } catch (err) { alert(err.message); btn.disabled = false; }
    });

    /* load AI suggestions */
    loadAiSuggestions(conv.id);
  }

  async function loadAiSuggestions(convId) {
    var list = document.getElementById("ai-suggestions");
    list.innerHTML = '<li class="skel skel--text"></li>'.repeat(2);
    try {
      var data = await A.api.request("GET", "/conversations/" + convId + "/ai-suggestions");
      list.innerHTML = "";
      data.items.forEach(function (s) {
        var li = document.createElement("li");
        li.className = "ai-suggestion";
        li.textContent = s;
        li.addEventListener("click", function () {
          var body = document.getElementById("compose-body");
          if (body) { body.value = s; body.focus(); }
        });
        list.appendChild(li);
      });
    } catch (e) {
      list.innerHTML = '<li class="empty-state--sm">' + e.message + '</li>';
    }
  }

  /* ---- load conversations ---- */
  async function loadConversations(filter) {
    var list = document.getElementById("conv-list");
    list.innerHTML = '<li class="skel skel--row"></li>'.repeat(6);
    try {
      var data = await A.api.request("GET", "/conversations", { query: { filter: filter } });
      conversations = data.items;
      renderConvList(conversations);

      /* open a specific one from URL */
      var qs = new URLSearchParams(location.search);
      var targetApproval = qs.get("approval");
      var targetProspect = qs.get("prospect");
      if (targetApproval || targetProspect) {
        var match = conversations[0];
        if (match) openConversation(match);
      }
    } catch (e) {
      document.getElementById("conv-list-state").innerHTML = '<p class="empty-state">' + e.message + '</p>';
    }
  }

  /* ---- send message ---- */
  async function sendMessage() {
    var body = document.getElementById("compose-body");
    var sendBtn = document.getElementById("send-btn");
    if (!body || !body.value.trim() || !activeConvId) return;
    sendBtn.disabled = true;
    try {
      await A.api.request("POST", "/conversations/" + activeConvId + "/messages", {
        body: { channel: document.getElementById("compose-channel").value, body: body.value.trim() }
      });
      body.value = "";
      /* refresh messages */
      var conv = conversations.find(function (c) { return c.id === activeConvId; });
      if (conv) openConversation(conv);
    } catch (e) {
      alert(e.message);
    } finally {
      sendBtn.disabled = false;
    }
  }

  /* ---- filter buttons ---- */
  function initFilters() {
    var group = document.getElementById("conv-filters");
    if (!group) return;
    group.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-filter]");
      if (!btn) return;
      group.querySelectorAll("[data-filter]").forEach(function (b) { b.classList.remove("filter--active"); });
      btn.classList.add("filter--active");
      activeFilter = btn.dataset.filter;
      loadConversations(activeFilter);
    });
  }

  /* ---- search ---- */
  function initSearch() {
    var input = document.getElementById("conv-search");
    if (!input) return;
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      var filtered = conversations.filter(function (c) {
        return (c.prospect.name + c.prospect.company + c.preview).toLowerCase().includes(q);
      });
      renderConvList(filtered);
    });
  }

  /* ---- mobile back ---- */
  function initMobileBack() {
    var btn = document.getElementById("inbox-back");
    if (!btn) return;
    btn.addEventListener("click", function () {
      document.getElementById("inbox-list-pane").classList.remove("inbox__list--hidden");
      btn.hidden = true;
      document.getElementById("thread-empty").hidden = false;
      document.getElementById("thread-content").hidden = true;
      activeConvId = null;
    });
  }

  /* ---- init ---- */
  document.addEventListener("DOMContentLoaded", function () {
    extendMock();
    initFilters();
    initSearch();
    initMobileBack();

    var qs = new URLSearchParams(location.search);
    var initFilter = qs.get("filter") || "all";
    var filterBtn = document.querySelector("[data-filter='" + initFilter + "']");
    if (filterBtn) {
      document.querySelectorAll("[data-filter]").forEach(function (b) { b.classList.remove("filter--active"); });
      filterBtn.classList.add("filter--active");
      activeFilter = initFilter;
    }
    loadConversations(activeFilter);

    var sendBtn = document.getElementById("send-btn");
    if (sendBtn) sendBtn.addEventListener("click", sendMessage);

    var aiDraftBtn = document.getElementById("ai-fill-btn");
    if (aiDraftBtn) {
      aiDraftBtn.addEventListener("click", async function () {
        if (!activeConvId) return;
        aiDraftBtn.disabled = true;
        try {
          var data = await A.api.request("GET", "/conversations/" + activeConvId + "/ai-suggestions");
          var body = document.getElementById("compose-body");
          if (body && data.items && data.items[0]) body.value = data.items[0];
        } catch (e) { alert(e.message); }
        aiDraftBtn.disabled = false;
      });
    }

    var refreshSug = document.getElementById("refresh-suggestions");
    if (refreshSug) {
      refreshSug.addEventListener("click", function () {
        if (activeConvId) loadAiSuggestions(activeConvId);
      });
    }
  });
})(window.Atlas);
