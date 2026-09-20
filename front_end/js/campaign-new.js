/* ==========================================================================
   campaign-new.js — multi-step campaign creation wizard
   ========================================================================== */
(function (A) {
  var currentStep = 1;
  var totalSteps = 3;
  var steps = [];
  var isEdit = false;

  /* ---- sequence builder ---- */
  var sequence = [];

  function renderSequence() {
    var builder = document.getElementById("sequence-builder");
    if (!builder) return;
    if (!sequence.length) {
      builder.innerHTML = '<p class="empty-state empty-state--sm">No steps yet. Add an email or delay below.</p>';
      return;
    }
    builder.innerHTML = sequence.map(function (s, i) {
      if (s.type === "email") {
        return '<div class="seq-step seq-step--email" data-idx="' + i + '">' +
          '<div class="seq-step__icon">✉</div>' +
          '<div class="seq-step__body">' +
            '<div class="seq-step__label">Email step ' + (i+1) + '</div>' +
            '<input class="input input--sm" type="text" value="' + esc(s.subject||"") + '" placeholder="Subject line" data-field="subject" data-idx="' + i + '">' +
            '<textarea class="input input--textarea input--sm" rows="2" placeholder="Email body or prompt…" data-field="body" data-idx="' + i + '">' + esc(s.body||"") + '</textarea>' +
          '</div>' +
          '<button type="button" class="btn btn--ghost btn--xs seq-step__remove" data-remove="' + i + '" aria-label="Remove step"><svg class="icon"><use href="#i-x"/></svg></button>' +
        '</div>';
      }
      return '<div class="seq-step seq-step--delay" data-idx="' + i + '">' +
        '<div class="seq-step__icon">⏱</div>' +
        '<div class="seq-step__body">' +
          '<div class="seq-step__label">Wait</div>' +
          '<div class="seq-step__delay-row">' +
            '<input class="input input--sm" type="number" value="' + (s.days||3) + '" min="1" max="30" data-field="days" data-idx="' + i + '" style="width:72px"> days' +
          '</div>' +
        '</div>' +
        '<button type="button" class="btn btn--ghost btn--xs seq-step__remove" data-remove="' + i + '" aria-label="Remove step"><svg class="icon"><use href="#i-x"/></svg></button>' +
      '</div>';
    }).join("");
  }

  function esc(s) { return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

  function initSequenceBuilder() {
    var builder = document.getElementById("sequence-builder");
    var addEmail = document.getElementById("add-email-step");
    var addDelay = document.getElementById("add-delay-step");
    if (!builder || !addEmail || !addDelay) return;

    /* seed with a default step */
    sequence = [
      { type: "email", subject: "Quick question for {{first_name}}", body: "" },
      { type: "delay", days: 3 },
      { type: "email", subject: "Following up — {{first_name}}", body: "" }
    ];
    renderSequence();

    addEmail.addEventListener("click", function () {
      sequence.push({ type: "email", subject: "", body: "" });
      renderSequence();
    });
    addDelay.addEventListener("click", function () {
      sequence.push({ type: "delay", days: 3 });
      renderSequence();
    });

    builder.addEventListener("click", function (e) {
      var rmBtn = e.target.closest("[data-remove]");
      if (rmBtn) {
        sequence.splice(parseInt(rmBtn.dataset.remove, 10), 1);
        renderSequence();
      }
    });
    builder.addEventListener("input", function (e) {
      var el = e.target;
      if (el.dataset.field && el.dataset.idx !== undefined) {
        sequence[parseInt(el.dataset.idx, 10)][el.dataset.field] = el.value;
      }
    });
  }

  /* ---- step navigation ---- */
  function showStep(n) {
    steps.forEach(function (panel, i) { panel.hidden = (i !== n - 1); });
    var fills = [33, 66, 100];
    var fill = document.getElementById("progress-fill");
    if (fill) fill.style.width = fills[n-1] + "%";
    document.querySelectorAll("[data-step]").forEach(function (el) {
      var s = parseInt(el.dataset.step, 10);
      el.classList.toggle("wizard-step--active", s === n);
      el.classList.toggle("wizard-step--done", s < n);
    });
    var btnBack = document.getElementById("btn-back");
    var btnNext = document.getElementById("btn-next");
    var btnCreate = document.getElementById("btn-create");
    if (btnBack) btnBack.hidden = (n === 1);
    if (btnNext) btnNext.hidden = (n === totalSteps);
    if (btnCreate) btnCreate.hidden = (n !== totalSteps);
    currentStep = n;
  }

  function validateStep(n) {
    if (n === 1) {
      var name = document.getElementById("f-name");
      var goal = document.getElementById("f-goal");
      if (!name || !name.value.trim()) { name && name.focus(); alert("Please enter a campaign name."); return false; }
      if (!goal || !goal.value) { goal && goal.focus(); alert("Please select a goal."); return false; }
    }
    return true;
  }

  /* ---- collect form data ---- */
  function collectData() {
    var industries = Array.from(document.querySelectorAll("[name=industry]:checked")).map(function (el) { return el.value; });
    var seniorities = Array.from(document.querySelectorAll("[name=seniority]:checked")).map(function (el) { return el.value; });
    var channels = Array.from(document.querySelectorAll("[name=channel]:checked")).map(function (el) { return el.value; });
    return {
      name: (document.getElementById("f-name")||{}).value || "",
      description: (document.getElementById("f-desc")||{}).value || "",
      goal: (document.getElementById("f-goal")||{}).value || "",
      start_date: (document.getElementById("f-start")||{}).value || null,
      owner_id: (document.getElementById("f-owner")||{}).value || "",
      icp: {
        industries: industries,
        employees_min: parseInt((document.getElementById("f-emp-min")||{}).value || "0", 10) || null,
        employees_max: parseInt((document.getElementById("f-emp-max")||{}).value || "0", 10) || null,
        seniority_levels: seniorities,
        keywords: ((document.getElementById("f-keywords")||{}).value || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean),
        funding_stage: (document.getElementById("f-funding")||{}).value || null,
        geography: (document.getElementById("f-geo")||{}).value || ""
      },
      outreach: {
        channels: channels,
        daily_limit: parseInt((document.getElementById("f-daily-limit")||{}).value || "50", 10),
        timezone: (document.getElementById("f-timezone")||{}).value || "America/New_York",
        approval_threshold: parseFloat((document.getElementById("f-approval-threshold")||{}).value || "0.8"),
        dry_run: (document.getElementById("f-dry-run")||{}).checked || false,
        sequence: sequence
      }
    };
  }

  /* ---- transform wizard data into the backend's CampaignCreate/Update shape ----
     The wizard's own collectData() shape (icp/outreach nested, channels as a
     flat list of type strings) is convenient for the form; the backend's
     Campaign model wants channels as full {type,state,daily_limit,config}
     objects and policy fields flattened into a single `policies` dict. */
  function buildBackendPayload(data, forCreate) {
    var payload = {
      name: data.name,
      description: data.description || null,
      icp: data.icp || {},
      objective: data.goal || null,
      policies: {
        daily_limit: data.outreach.daily_limit,
        approval_threshold: data.outreach.approval_threshold,
        dry_run: data.outreach.dry_run,
        timezone: data.outreach.timezone,
        sequence: data.outreach.sequence
      }
    };
    if (data.owner_id) payload.owner_id = data.owner_id;
    if (forCreate) {
      payload.channels = (data.outreach.channels || []).map(function (type) {
        return { type: type, state: "active", daily_limit: data.outreach.daily_limit || 50, config: {} };
      });
    }
    return payload;
  }

  /* ---- create / update ---- */
  async function submitCampaign() {
    var btn = document.getElementById("btn-create");
    if (btn) { btn.disabled = true; btn.textContent = "Creating…"; }
    try {
      var data = collectData();
      var editId = new URLSearchParams(location.search).get("id");
      if (editId) {
        /* This wizard does not (yet) preload the campaign's existing ICP,
           channels, or policy fields when opened for editing — the form
           starts from its own defaults. Sending those defaults as-is would
           silently overwrite the campaign's real configuration, and sending
           `icp` at all on a *live* campaign is refused by the backend unless
           explicitly forced (changing the ICP of a live campaign re-scores
           prospects mid-flight). So an edit here only updates name,
           description, objective, owner and the outreach-policy fields the
           wizard actually presents — ICP and channels are left untouched. */
        var editPayload = buildBackendPayload(data, false);
        delete editPayload.icp;
        await A.api.request("PUT", "/campaigns/" + encodeURIComponent(editId), { body: editPayload });
        window.location.href = Atlas.config.ROUTES.campaign(editId);
      } else {
        /* mock: just redirect to campaigns list */
        if (Atlas.config.USE_MOCK) {
          await new Promise(function (r) { setTimeout(r, 400); });
          window.location.href = Atlas.config.ROUTES.campaigns;
          return;
        }
        var createPayload = buildBackendPayload(data, true);
        var res = await A.api.request("POST", "/campaigns", { body: createPayload });
        window.location.href = Atlas.config.ROUTES.campaign(res.id);
      }
    } catch (e) {
      alert("Could not save campaign: " + e.message);
      if (btn) { btn.disabled = false; btn.innerHTML = '<svg class="icon"><use href="#i-campaigns"/></svg>Create campaign'; }
    }
  }

  /* ---- init ---- */
  document.addEventListener("DOMContentLoaded", function () {
    steps = Array.from(document.querySelectorAll(".wizard-panel"));
    initSequenceBuilder();
    showStep(1);

    var editId = new URLSearchParams(location.search).get("id");
    if (editId) {
      isEdit = true;
      document.getElementById("wizard-title").textContent = "Edit campaign";
      document.title = "Edit Campaign · Atlas SDR";
    }

    document.getElementById("btn-next").addEventListener("click", function () {
      if (!validateStep(currentStep)) return;
      if (currentStep < totalSteps) showStep(currentStep + 1);
    });
    document.getElementById("btn-back").addEventListener("click", function () {
      if (currentStep > 1) showStep(currentStep - 1);
    });
    document.getElementById("btn-create").addEventListener("click", submitCampaign);
  });
})(window.Atlas);
