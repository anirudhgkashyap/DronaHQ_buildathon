# Atlas SDR control plane: frontend

Static HTML, one shared CSS file and a small vanilla-JS layer. No build step, no framework, no dependencies. Open `campaigns.html` in a browser and it runs on mock data. To go live, edit `js/config.js` and implement the endpoints below.

Pages built so far: **`campaigns.html`** (campaigns home). More pages will reuse `css/styles.css`, `js/shell.js`, `js/api.js` and `js/config.js` unchanged.

## Folder structure

```
atlas-frontend/
├── campaigns.html        Campaigns home page
├── css/
│   └── styles.css        All styles: tokens, shell, components, page styles
└── js/
    ├── config.js         Backend URL, auth, mock switch, page routes. Backend team edits this.
    ├── api.js            Every network call. One function per endpoint. No page calls fetch().
    ├── mock-data.js      Fake backend for local work. Same paths and JSON as the real API. Delete when live.
    ├── shell.js          Shared: formatting helpers, confirm dialog, toasts, global kill switch, avatar
    └── campaigns.js      Campaigns page: loading, rendering, lifecycle actions, filters
```

Script order in the HTML matters: `config → mock-data → api → shell → page`.

## Run locally

Double-click `campaigns.html`, or serve the folder (`python3 -m http.server 8080`). Google Fonts loads from the network; the page falls back to system fonts offline.

## Connect to the real backend

1. In `js/config.js` set `USE_MOCK: false`.
2. Set `API_BASE_URL` to your API root (default `/api/v1`, so `GET /api/v1/campaigns`).
3. Set auth: return a token from `getAuthToken()` (sent as `Authorization: Bearer <token>`), **or** set `SEND_COOKIES: true` for session cookies (the server must then send CORS `Access-Control-Allow-Credentials: true` and a specific, non-wildcard origin).
4. Point `onUnauthorized` at your login page. It runs on any `401`.
5. Remove the `<script src="js/mock-data.js">` tag once you no longer need mocks.
6. If the frontend and API are on different origins, enable CORS for the frontend origin on the API.

The frontend does not decide what is allowed. It shows the buttons for each status, and the server accepts or rejects the action.

## API contract

All requests and responses are JSON. All timestamps are ISO 8601 UTC strings. IDs are opaque strings.

### Error format (any non-2xx)

```json
{ "error": { "code": "invalid_transition", "message": "Only a live campaign can be paused." } }
```

`message` is shown to the user verbatim in a toast, so write it for a sales manager, not a developer. If `error.message` is missing the UI shows a generic message.

### Enums

| Field | Values |
|---|---|
| `campaign.status` | `draft`, `live`, `paused`, `completed`, `archived` |
| `channel.type` | `linkedin`, `email`, `voice`, `sms`, `whatsapp` |
| `channel.state` | `active`, `paused`, `off` (`paused` = that channel alone is paused) |
| `event.type` | `info`, `success`, `warning`, `error` |

### `GET /me`

```json
{ "id": "usr_sm", "name": "S. Menon", "initials": "SM", "role": "manager" }
```

Used for the avatar in the top bar.

### `GET /campaigns`

Returns **all** campaigns including archived. Filtering, search and sorting happen in the browser (the list is expected to be dozens, not thousands). If that changes, add query params and update `loadCampaigns` in `js/campaigns.js`.

```json
{
  "items": [
    {
      "id": "cmp_us_saas_cto",
      "name": "US SaaS · CTO Outreach",
      "label": "Campaign A",
      "icp_summary": "SaaS, Series B–D, CTO / VP Eng, 50–500 FTE",
      "status": "live",
      "owner": { "id": "usr_mr", "name": "M. Rao" },
      "channels": [
        { "type": "linkedin", "state": "active" },
        { "type": "email", "state": "active" },
        { "type": "sms", "state": "paused" }
      ],
      "metrics": { "prospects": 1284, "outreach": 426, "meetings": 18 },
      "pending_approvals": 3,
      "created_at": "2026-08-12T09:00:00Z",
      "updated_at": "2026-09-19T11:02:00Z",
      "paused_at": null,
      "paused_by": null
    }
  ]
}
```

Field notes:

- `metrics.prospects` = qualified prospects in the campaign, `outreach` = prospects contacted, `meetings` = meetings booked. The UI derives "33% of prospects" and "4.2% of outreach" from these, so keep the definitions consistent with the campaign dashboard funnel.
- `owner` may be `null` (shows "Unassigned").
- `channels` may be `[]` (shows "Not configured", typical for drafts).
- `paused_at` and `paused_by` (`{ id, name }`) are only set while `status` is `paused`. They drive the "Paused 3 hr ago by A. Iyer" line.
- `pending_approvals` is the count of open human approvals for this campaign. `0` hides the "to review" tag.
- `created_at` is the default sort order.

### `GET /campaigns/summary`

```json
{
  "live_campaigns": 3,
  "total_campaigns": 4,
  "prospects_in_motion": 2315,
  "prospects_in_motion_delta_today": 184,
  "meetings_30d": 38,
  "meetings_30d_delta_wow": 6,
  "pending_approvals": 7
}
```

`total_campaigns` should exclude archived. Deltas are signed integers (negative allowed). `pending_approvals` also feeds the badge on the Conversations nav link.

### `POST /campaigns/:id/{action}`

`action` is one of `pause`, `resume`, `complete`, `archive`, `duplicate`. No request body. Returns the **updated campaign** (same shape as a list item). For `duplicate`, returns the **new** campaign (status `draft`).

| Action | Allowed from | Result |
|---|---|---|
| `pause` | `live` | `paused`. Autonomous execution stops immediately, data is kept. Set `paused_at`, `paused_by`. |
| `resume` | `paused` | `live` |
| `complete` | `live`, `paused` | `completed` |
| `archive` | any except `archived` | `archived` |
| `duplicate` | any | new `draft` copy |

Return `409` with code `invalid_transition` when the campaign is in the wrong state (for example, a colleague already paused it). On any failure the UI shows the message and re-fetches the list, so the server's state wins.

**Activate** is deliberately not an API call from this page. The "Activate" button on a draft opens the review step (`campaign-new.html?id=…&step=review`) so the manager sees the full setup before going live. The `activate` call will belong to that page.

### `GET /approvals?status=pending&limit=4`

```json
{
  "items": [
    {
      "id": "apr_1",
      "campaign_id": "cmp_voice_ai_founders",
      "campaign_name": "Voice AI Founders",
      "title": "Voice SDR Agent flagged a pricing objection",
      "reason": "Needs human reply",
      "created_at": "2026-09-19T11:58:00Z"
    }
  ],
  "total": 7
}
```

Newest first. `total` is the full pending count (may exceed `items.length`). `reason` is a short label such as "Needs human reply", "Above seniority threshold", "Low confidence" (the confidence gate), or "Conflict". `title` is one plain sentence.

### `GET /events?limit=5`

```json
{
  "items": [
    {
      "id": "evt_1",
      "type": "info",
      "campaign_name": "India BFSI · CIO Outreach",
      "message": "paused by A. Iyer. All autonomous execution stopped.",
      "created_at": "2026-09-19T09:02:00Z"
    }
  ]
}
```

Newest first. The UI renders `campaign_name` in bold followed by `message`, so write `message` as a continuation ("paused by …"). `campaign_name` may be omitted for platform-wide events.

### `GET /platform/kill-switch` and `PUT /platform/kill-switch`

```json
{ "engaged": false, "engaged_at": null, "engaged_by": null }
```

`PUT` body: `{ "engaged": true }` (stop everything) or `{ "engaged": false }` (resume). Returns the new state. While engaged, the server must stop **all** autonomous external actions on every campaign and channel, without changing campaign statuses. The UI shows a red banner, changes the sidebar control to "Resume platform", and marks Live rows "Halted by kill switch".

## How the page behaves (so backend and QA agree)

- **Lifecycle rail.** Each row has a left-edge rail: solid green (Live), amber hatch (Paused), dashed grey (Draft), purple (Completed), light grey (Archived). Paused rows are also tinted.
- **Pause** asks for confirmation, then shows an in-row spinner until the server responds. **Resume** does not ask.
- **Complete** and **Archive** ask for confirmation. **Duplicate** does not, and offers an "Open copy" link.
- **"All" tab hides archived campaigns.** They appear under the Archived tab (only shown when at least one exists). Completed works the same way.
- **Compare** needs 2 to 4 selected campaigns and links to `campaign-compare.html?ids=a,b,c`.
- **Polling.** The page silently refreshes every `POLL_INTERVAL_MS` (default 30 s) so a pause by a colleague appears without a reload. It skips a refresh while a dialog, menu or action is open. Set it to `0` to disable.
- **Failures are contained.** The campaigns table, summary strip, approvals and activity panels each load independently and show their own error and "Try again" button. One failed endpoint never blanks the page.

## Routes the page links to (pages still to build)

Change any of these in `Atlas.config.ROUTES`.

| Link | URL |
|---|---|
| New campaign | `campaign-new.html` |
| Campaign dashboard | `campaign-detail.html?id=<id>` |
| Edit configuration | `campaign-new.html?id=<id>` |
| Review and activate a draft | `campaign-new.html?id=<id>&step=review` |
| Compare | `campaign-compare.html?ids=<id>,<id>` |
| Approvals / conversations | `conversations.html`, `conversations.html?filter=approvals`, `conversations.html?approval=<id>` |
| Global settings | `settings.html` |

## Adding another page

1. Copy the sidebar block from `campaigns.html` (keep the `data-killswitch` markup and `data-slot="platform-banner"`). Move `aria-current="page"` to the right link.
2. Include the same stylesheet and the scripts in order: `config`, `mock-data`, `api`, `shell`, then your page script.
3. Add new endpoints as functions in `js/api.js` and matching handlers in `js/mock-data.js`.
4. Reuse the components in `css/styles.css` (`.btn`, `.pill`, `.panel`, `.tag`, `.feed__item`, `.state`, `.dialog`, toasts). Lifecycle colours are CSS variables (`--live`, `--paused`, `--draft`, `--done`, `--archived`) so a status looks identical everywhere.

## Security notes

- Every server value is passed through `Atlas.fmt.esc()` before it goes into `innerHTML`. Keep doing this for any new template.
- Tokens: `getAuthToken()` defaults to `localStorage`. Prefer an httpOnly session cookie (`SEND_COOKIES: true`) if the deployment allows it.
- The UI hides actions that do not apply to a status, but that is a convenience. Enforce permissions and valid transitions on the server.

## Accessibility and responsiveness

Keyboard focus is always visible. The row menu supports arrow keys and Escape. Status is never colour-only (each pill has a label and its own shape, channel states include screen-reader text). Motion respects `prefers-reduced-motion`. Below 860 px the table becomes stacked cards and the sidebar collapses to a top bar, with the kill switch always in view.
