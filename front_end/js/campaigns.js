/* ==========================================================================
   campaigns.js: campaigns home page.
   Loads data through Atlas.api, renders it, and sends lifecycle actions back.
   No business rules live here: the server decides what is allowed.
   ========================================================================== */
(function (A) {
  "use strict";
  var api = A.api, ui = A.ui, cfg = A.config, esc = A.fmt.esc, num = A.fmt.num, pct = A.fmt.pct, timeAgo = A.fmt.timeAgo;

  var STATUS_LABEL = { live: "Live", paused: "Paused", draft: "Draft", completed: "Completed", archived: "Archived" };
  var CHANNEL_LABEL = { linkedin: "LinkedIn", email: "Email", voice: "Voice", sms: "SMS", whatsapp: "WhatsApp" };
  var CHANNEL_STATE_LABEL = { active: "active", paused: "paused", off: "off" };
  var MAX_COMPARE = 4;

  var state = {
    campaigns: [], loaded: false,
    status: "all", owner: "all", sort: "created", q: "",
    selected: new Set(), platformStopped: false,
    busy: new Map() // campaign id -> action in flight
  };

  var $ = function (s) { return document.querySelector(s); };
  var els = {
    rows: $("#campaign-rows"), listState: $("#list-state"), filters: $("#filters"),
    owner: $("#owner-filter"), sort: $("#sort"), search: $("#search"),
    compare: $("#compare"), compareLabel: $("#compare-label"),
    menu: $("#row-menu"),
    approvals: $("#approvals-list"), approvalsCount: $("#approvals-count"), events: $("#events-list")
  };

  /* ================= Data loading ================= */

  async function loadCampaigns(quiet) {
    if (!quiet && !state.loaded) renderSkeleton();
    try {
      var res = await api.listCampaigns();
      state.campaigns = res.items || [];
      state.loaded = true;
      state.loadError = null;
    } catch (e) {
      if (quiet && state.loaded) return; // keep showing what we have
      state.loadError = e.message;
    }
    renderAll();
  }

  async function loadSummary() {
    try { renderSummary(await api.getSummary()); }
    catch (e) { renderSummary(null); }
  }

  async function loadApprovals() {
    try {
      var r = await api.listApprovals({ limit: 4 });
      renderApprovals(r.items || [], r.total || 0);
    } catch (e) { renderPanelError(els.approvals, "approvals", e.message); }
  }

  async function loadEvents() {
    try { renderEvents((await api.listEvents({ limit: 5 })).items || []); }
    catch (e) { renderPanelError(els.events, "events", e.message); }
  }

  function loadAll(quiet) {
    loadCampaigns(quiet); loadSummary(); loadApprovals(); loadEvents();
  }

  /* ================= Derived data ================= */

  function byId(id) { return state.campaigns.filter(function (c) { return c.id === id; })[0]; }

  function visible() {
    var q = state.q;
    var list = state.campaigns.filter(function (c) {
      if (state.status === "all") { if (c.status === "archived") return false; }
      else if (c.status !== state.status) return false;
      if (state.owner !== "all" && (!c.owner || c.owner.id !== state.owner)) return false;
      if (q) {
        var hay = [c.name, c.label, c.icp_summary, c.owner && c.owner.name].join(" ").toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    list.sort(function (a, b) {
      if (state.sort === "meetings") return (b.metrics.meetings - a.metrics.meetings) || a.name.localeCompare(b.name);
      if (state.sort === "name") return a.name.localeCompare(b.name);
      return new Date(a.created_at) - new Date(b.created_at);
    });
    return list;
  }

  function statusCounts() {
    var c = { all: 0, live: 0, paused: 0, draft: 0, completed: 0, archived: 0 };
    state.campaigns.forEach(function (x) {
      c[x.status] = (c[x.status] || 0) + 1;
      if (x.status !== "archived") c.all++;
    });
    return c;
  }

  /* ================= Rendering ================= */

  function renderAll() {
    renderFilters(); renderOwners(); renderRows(); renderCompare();
  }

  function renderFilters() {
    var counts = statusCounts();
    var tabs = ["all", "live", "paused", "draft", "completed", "archived"].filter(function (s) {
      return s === "all" || s === "live" || s === "paused" || s === "draft" || counts[s] > 0;
    });
    els.filters.innerHTML = tabs.map(function (s) {
      return '<button type="button" class="filter" data-status="' + s + '" aria-pressed="' + (state.status === s) + '">' +
        (s === "all" ? "All" : STATUS_LABEL[s]) + '<span class="filter__count">' + counts[s] + "</span></button>";
    }).join("");
  }

  function renderOwners() {
    var seen = {}, opts = ['<option value="all">All owners</option>'];
    state.campaigns.forEach(function (c) {
      if (c.owner && !seen[c.owner.id]) {
        seen[c.owner.id] = 1;
        opts.push('<option value="' + esc(c.owner.id) + '">' + esc(c.owner.name) + "</option>");
      }
    });
    els.owner.innerHTML = opts.join("");
    els.owner.value = seen[state.owner] ? state.owner : "all";
    if (!seen[state.owner]) state.owner = "all";
  }

  function renderSkeleton() {
    var row = '<tr class="c-row" style="cursor:default"><td class="c-select"></td><td><span class="skel" style="width:70%"></span></td>' +
      '<td><span class="skel" style="width:60px"></span></td><td><span class="skel" style="width:120px"></span></td>' +
      '<td><span class="skel" style="width:40px;margin-left:auto"></span></td><td><span class="skel" style="width:40px;margin-left:auto"></span></td>' +
      '<td><span class="skel" style="width:30px;margin-left:auto"></span></td><td><span class="skel" style="width:60px"></span></td><td></td></tr>';
    els.rows.innerHTML = row + row + row;
    els.listState.innerHTML = "";
  }

  function renderRows() {
    if (state.loadError) {
      els.rows.innerHTML = "";
      els.listState.innerHTML = '<div class="state"><div class="state__title">Couldn\'t load campaigns</div>' +
        "<p>" + esc(state.loadError) + '</p><button type="button" class="btn btn--secondary" data-retry="campaigns">Try again</button></div>';
      return;
    }
    var list = visible();
    if (!list.length) {
      els.rows.innerHTML = "";
      els.listState.innerHTML = state.campaigns.length === 0
        ? '<div class="state"><div class="state__title">No campaigns yet</div><p>A campaign targets one ICP with its own agents, prompts and channels.</p>' +
          '<a class="btn btn--primary" href="' + esc(cfg.ROUTES.create) + '">Create your first campaign</a></div>'
        : '<div class="state"><div class="state__title">No campaigns match</div><p>Try a different status, owner or search.</p>' +
          '<button type="button" class="btn btn--secondary" data-reset-filters>Clear filters</button></div>';
      return;
    }
    els.listState.innerHTML = "";
    els.rows.innerHTML = list.map(rowHTML).join("");
  }

  function channelsHTML(c) {
    if (!c.channels || !c.channels.length) return '<span class="muted">Not configured</span>';
    return '<div class="chans">' + c.channels.map(function (ch) {
      var name = CHANNEL_LABEL[ch.type] || ch.type;
      var st = CHANNEL_STATE_LABEL[ch.state] ? ch.state : "off";
      return '<span class="chan chan--' + st + '">' + esc(name) +
        (st !== "active" ? '<span class="sr-only"> (' + st + ")</span>" : "") + "</span>";
    }).join("") + "</div>";
  }

  function statusNote(c) {
    if (c.status === "paused") {
      return "Paused " + timeAgo(c.paused_at) + (c.paused_by ? " by " + c.paused_by.name : "");
    }
    if (c.status === "draft") return "Not sending";
    if (c.status === "live" && state.platformStopped) return "Halted by kill switch";
    return "";
  }

  function primaryAction(c) {
    var busy = state.busy.get(c.id);
    if (c.status === "live") {
      return '<button type="button" class="btn btn--pause btn--sm' + (busy ? " is-busy" : "") + '" data-action="pause"' + (busy ? " disabled" : "") + ">" +
        (busy ? "Pausing…" : '<svg class="icon"><use href="#i-pause"/></svg>Pause') + "</button>";
    }
    if (c.status === "paused") {
      return '<button type="button" class="btn btn--primary btn--sm' + (busy ? " is-busy" : "") + '" data-action="resume"' + (busy ? " disabled" : "") + ">" +
        (busy ? "Resuming…" : '<svg class="icon"><use href="#i-play"/></svg>Resume') + "</button>";
    }
    if (c.status === "draft") {
      return '<a class="btn btn--primary btn--sm" href="' + esc(cfg.ROUTES.review(c.id)) + '" title="Review the setup, then activate">Activate</a>';
    }
    return "";
  }

  function rowHTML(c) {
    var m = c.metrics || { prospects: 0, outreach: 0, meetings: 0 };
    var owner = c.owner ? esc(c.owner.name) : '<span class="muted">Unassigned</span>';
    var note = statusNote(c);
    var checked = state.selected.has(c.id);
    var lockCheck = !checked && state.selected.size >= MAX_COMPARE;
    var busy = state.busy.has(c.id);
    return '<tr class="c-row" data-id="' + esc(c.id) + '" data-status="' + esc(c.status) + '">' +
      '<td class="c-select"><input type="checkbox" data-select aria-label="Select ' + esc(c.name) + ' to compare"' +
        (checked ? " checked" : "") + (lockCheck ? " disabled" : "") + "></td>" +
      '<td class="c-campaign"><div class="c-meta"><a class="c-name" href="' + esc(cfg.ROUTES.campaign(c.id)) + '">' + esc(c.name) + "</a>" +
        (c.pending_approvals > 0 ? '<a class="tag tag--warn" href="' + esc(cfg.ROUTES.approvals) + '" data-stop>' + c.pending_approvals + " to review</a>" : "") +
      '</div><div class="c-sub">' + esc(c.icp_summary || "") + "</div></td>" +
      '<td data-label="Status"><span class="pill pill--' + esc(c.status) + '">' + esc(STATUS_LABEL[c.status] || c.status) + "</span>" +
        (note ? '<div class="c-status-note">' + esc(note) + "</div>" : "") + "</td>" +
      '<td data-label="Channels">' + channelsHTML(c) + "</td>" +
      '<td class="num" data-label="Prospects"><div class="stat-num">' + num(m.prospects) + "</div></td>" +
      '<td class="num" data-label="Outreach"><div class="stat-num">' + num(m.outreach) + '</div><div class="stat-sub">' + (m.prospects ? pct(m.outreach, m.prospects) + " of prospects" : "&nbsp;") + "</div></td>" +
      '<td class="num" data-label="Meetings"><div class="stat-num">' + num(m.meetings) + '</div><div class="stat-sub">' + (m.outreach ? pct(m.meetings, m.outreach) + " of outreach" : "&nbsp;") + "</div></td>" +
      '<td class="c-owner" data-label="Owner">' + owner + "</td>" +
      '<td class="c-actions">' + primaryAction(c) +
        ' <button type="button" class="btn btn--secondary btn--sm btn--icon" data-action="menu" aria-haspopup="menu" aria-expanded="false" aria-label="More actions for ' + esc(c.name) + '"' + (busy ? " disabled" : "") + '><svg class="icon"><use href="#i-more"/></svg></button></td>' +
      "</tr>";
  }

  function renderCompare() {
    var n = state.selected.size;
    var ok = n >= 2;
    els.compare.setAttribute("aria-disabled", ok ? "false" : "true");
    els.compare.href = ok ? cfg.ROUTES.compare(Array.from(state.selected)) : "#";
    els.compareLabel.textContent = n ? "Compare (" + n + ")" : "Compare";
  }

  function setMetric(key, value, note, cls) {
    var v = document.querySelector('[data-metric="' + key + '"]');
    var n = document.querySelector('[data-metric-note="' + key + '"]');
    if (v) v.textContent = value;
    if (n) { n.textContent = note || ""; n.className = "metric__note" + (cls ? " metric__note--" + cls : ""); }
  }

  function renderSummary(s) {
    if (!s) {
      ["live", "prospects", "meetings", "approvals"].forEach(function (k) { setMetric(k, "–", k === "live" ? "Couldn't load" : ""); });
      return;
    }
    setMetric("live", num(s.live_campaigns), "of " + num(s.total_campaigns) + " campaigns");
    var pd = s.prospects_in_motion_delta_today;
    setMetric("prospects", num(s.prospects_in_motion), pd ? A.fmt.signed(pd) + " today" : "No change today", pd > 0 ? "up" : pd < 0 ? "down" : "");
    var md = s.meetings_30d_delta_wow;
    setMetric("meetings", num(s.meetings_30d), md ? A.fmt.signed(md) + " vs last week" : "Same as last week", md > 0 ? "up" : md < 0 ? "down" : "");
    setMetric("approvals", num(s.pending_approvals), s.pending_approvals > 0 ? "Agents are waiting on you" : "Nothing waiting", s.pending_approvals > 0 ? "" : "");
    var tile = document.getElementById("metric-approvals");
    if (tile) tile.classList.toggle("metric--alert", s.pending_approvals > 0);
    var badge = document.querySelector('[data-badge="approvals"]');
    if (badge) {
      badge.textContent = s.pending_approvals;
      badge.classList.toggle("is-visible", s.pending_approvals > 0);
    }
  }

  function renderApprovals(items, total) {
    els.approvalsCount.hidden = !total;
    els.approvalsCount.textContent = total;
    if (!items.length) {
      els.approvals.innerHTML = '<li class="state state--compact"><div class="state__title">Nothing waiting on you</div><p>Agents are working within their rules.</p></li>';
      return;
    }
    els.approvals.innerHTML = items.map(function (a) {
      return '<li class="feed__item"><span class="dot dot--warning"></span><div class="feed__main">' +
        "<div><strong>" + esc(a.campaign_name) + "</strong>: " + esc(a.title) + "</div>" +
        '<div class="feed__meta"><span class="tag tag--warn">' + esc(a.reason) + "</span>" + esc(timeAgo(a.created_at)) + "</div></div>" +
        '<a class="btn btn--secondary btn--sm" href="' + esc(cfg.ROUTES.approval(a.id)) + '">Review</a></li>';
    }).join("") + (total > items.length
      ? '<li class="feed__item"><a href="' + esc(cfg.ROUTES.approvals) + '">See all ' + total + " pending approvals</a></li>" : "");
  }

  function renderEvents(items) {
    if (!items.length) {
      els.events.innerHTML = '<li class="state state--compact"><div class="state__title">No recent activity</div><p>Pauses, resumes and prompt changes show up here.</p></li>';
      return;
    }
    els.events.innerHTML = items.map(function (e) {
      return '<li class="feed__item"><span class="dot dot--' + esc(e.type || "info") + '"></span><div class="feed__main">' +
        "<div>" + (e.campaign_name ? "<strong>" + esc(e.campaign_name) + "</strong> " : "") + esc(e.message) + "</div>" +
        '<div class="feed__meta">' + esc(timeAgo(e.created_at)) + "</div></div></li>";
    }).join("");
  }

  function renderPanelError(listEl, key, message) {
    listEl.innerHTML = '<li class="state state--compact"><div class="state__title">Couldn\'t load this</div><p>' + esc(message) +
      '</p><button type="button" class="btn btn--secondary btn--sm" data-retry="' + key + '">Try again</button></li>';
  }

  /* ================= Actions ================= */

  var CONFIRMS = {
    pause: function (c) {
      return { title: "Pause \u201c" + c.name + "\u201d?", tone: "warn", confirmLabel: "Pause campaign",
        message: "All autonomous execution stops immediately and no new outreach fires. Prospects and conversations are kept, and you can resume at any time. Other campaigns keep running." };
    },
    complete: function (c) {
      return { title: "Mark \u201c" + c.name + "\u201d as completed?", tone: "primary", confirmLabel: "Mark as completed",
        message: "The campaign stops running. Its history, decisions and analytics stay available." };
    },
    archive: function (c) {
      return { title: "Archive \u201c" + c.name + "\u201d?", tone: "danger", confirmLabel: "Archive",
        message: "The campaign stops running and leaves the main list. History and analytics stay available under the Archived filter." };
    }
  };
  var DONE = { pause: "paused", resume: "resumed", complete: "marked as completed", archive: "archived" };
  var BUSY_FOCUS = null;

  async function runAction(id, action) {
    var c = byId(id);
    if (!c || state.busy.has(id)) return;
    if (CONFIRMS[action]) {
      var ok = await ui.confirm(CONFIRMS[action](c));
      if (!ok) return;
    }
    state.busy.set(id, action);
    renderRows();
    try {
      var res = await api.campaignAction(id, action);
      if (action === "duplicate") {
        state.campaigns.push(res);
        ui.toast("Duplicated as \u201c" + res.name + "\u201d", { action: { label: "Open copy", href: cfg.ROUTES.edit(res.id) } });
      } else {
        state.campaigns = state.campaigns.map(function (x) { return x.id === id ? res : x; });
        ui.toast("\u201c" + c.name + "\u201d " + DONE[action] + ".");
      }
    } catch (e) {
      ui.toast(e.message, { tone: "error" });
      loadCampaigns(true); // the server's state wins if it disagrees with ours
    } finally {
      state.busy.delete(id);
      renderAll();
      var again = els.rows.querySelector('[data-id="' + id + '"] .btn--primary, [data-id="' + id + '"] .btn--pause');
      if (again && BUSY_FOCUS === id) again.focus();
      BUSY_FOCUS = null;
      loadSummary(); loadEvents();
    }
  }

  /* ---- Row menu ---- */
  var menuFor = null, menuBtn = null;

  function menuItems(c) {
    var items = [
      { label: "Open dashboard", href: cfg.ROUTES.campaign(c.id) },
      { label: "Edit configuration", href: cfg.ROUTES.edit(c.id) },
      { label: "Duplicate", action: "duplicate" }
    ];
    var end = [];
    if (c.status === "live" || c.status === "paused") end.push({ label: "Mark as completed", action: "complete" });
    if (c.status !== "archived") end.push({ label: "Archive", action: "archive", danger: true });
    if (end.length) items.push({ sep: true }); 
    return items.concat(end);
  }

  function openMenu(btn) {
    var c = byId(btn.closest("tr").dataset.id);
    if (!c) return;
    closeMenu();
    menuFor = c.id; menuBtn = btn;
    els.menu.innerHTML = menuItems(c).map(function (it) {
      if (it.sep) return '<div class="menu__sep" role="separator"></div>';
      var cls = "menu__item" + (it.danger ? " menu__item--danger" : "");
      return it.href
        ? '<a class="' + cls + '" role="menuitem" href="' + esc(it.href) + '">' + esc(it.label) + "</a>"
        : '<button type="button" class="' + cls + '" role="menuitem" data-menu-action="' + it.action + '">' + esc(it.label) + "</button>";
    }).join("");
    els.menu.hidden = false;
    var r = btn.getBoundingClientRect(), mh = els.menu.offsetHeight, mw = els.menu.offsetWidth;
    var top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
    els.menu.style.top = top + "px";
    els.menu.style.left = Math.max(8, r.right - mw) + "px";
    btn.setAttribute("aria-expanded", "true");
    els.menu.querySelector(".menu__item").focus();
  }

  function closeMenu(returnFocus) {
    if (els.menu.hidden) return;
    els.menu.hidden = true;
    if (menuBtn) { menuBtn.setAttribute("aria-expanded", "false"); if (returnFocus) menuBtn.focus(); }
    menuFor = null; menuBtn = null;
  }

  /* ================= Events ================= */

  els.rows.addEventListener("click", function (e) {
    if (e.target.closest("[data-stop]")) return;
    var tr = e.target.closest("tr.c-row");
    if (!tr) return;
    var btn = e.target.closest("[data-action]");
    if (btn) {
      var action = btn.dataset.action;
      if (action === "menu") {
        if (menuBtn === btn) closeMenu(); else openMenu(btn);
        return;
      }
      BUSY_FOCUS = tr.dataset.id;
      runAction(tr.dataset.id, action);
      return;
    }
    if (e.target.closest("a, input, button, label")) return;
    window.location.href = cfg.ROUTES.campaign(tr.dataset.id);
  });

  els.rows.addEventListener("change", function (e) {
    var box = e.target.closest("[data-select]");
    if (!box) return;
    var id = box.closest("tr").dataset.id;
    if (box.checked) state.selected.add(id); else state.selected.delete(id);
    renderRows(); renderCompare();
  });

  els.menu.addEventListener("click", function (e) {
    var b = e.target.closest("[data-menu-action]");
    if (!b) return;
    var id = menuFor, action = b.dataset.menuAction;
    closeMenu();
    runAction(id, action);
  });
  els.menu.addEventListener("keydown", function (e) {
    var items = Array.from(els.menu.querySelectorAll(".menu__item"));
    var i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); closeMenu(true); }
    else if (e.key === "Tab") closeMenu();
  });
  document.addEventListener("click", function (e) {
    if (!els.menu.hidden && !e.target.closest("#row-menu") && !e.target.closest('[data-action="menu"]')) closeMenu();
  });
  window.addEventListener("resize", function () { closeMenu(); });
  window.addEventListener("scroll", function () { closeMenu(); }, true);

  els.filters.addEventListener("click", function (e) {
    var b = e.target.closest("[data-status]");
    if (!b) return;
    state.status = b.dataset.status;
    renderFilters(); renderRows();
  });
  els.owner.addEventListener("change", function () { state.owner = els.owner.value; renderRows(); });
  els.sort.addEventListener("change", function () { state.sort = els.sort.value; renderRows(); });

  var searchTimer;
  els.search.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { state.q = els.search.value.trim().toLowerCase(); renderRows(); }, 120);
  });

  els.compare.addEventListener("click", function (e) {
    if (els.compare.getAttribute("aria-disabled") === "true") e.preventDefault();
  });

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-reset-filters]")) {
      state.status = "all"; state.owner = "all"; state.q = ""; els.search.value = "";
      renderAll();
    }
    var retry = e.target.closest("[data-retry]");
    if (retry) {
      var k = retry.dataset.retry;
      if (k === "campaigns") { state.loadError = null; loadCampaigns(); }
      else if (k === "approvals") loadApprovals();
      else if (k === "events") loadEvents();
    }
  });

  /* Background refresh so a pause made by a colleague shows up without a reload */
  if (cfg.POLL_INTERVAL_MS > 0) {
    setInterval(function () {
      var dialogOpen = document.querySelector("dialog[open]");
      if (document.hidden || dialogOpen || state.busy.size || !els.menu.hidden) return;
      loadAll(true);
    }, cfg.POLL_INTERVAL_MS);
  }

  /* Kill switch lives in shell.js; mirror its state so Live rows read as halted */
  document.addEventListener("atlas:killswitch", function (e) {
    state.platformStopped = !!e.detail.engaged;
    document.body.classList.toggle("is-platform-stopped", state.platformStopped);
    if (state.loaded) renderRows();
  });

  loadAll(false);
})(window.Atlas);
