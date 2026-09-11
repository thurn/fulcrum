# Fulcrum Dashboard and Design System

The **Fulcrum dashboard** is a read-only view of a local Codex agent fleet,
managed projects, and Beads work. It should feel like a science-fantasy command
center while remaining comfortable for sustained reading. Activity is visible
through restrained motion, clear labels, and meaningful information hierarchy.

## Related Information

These references explain where the displayed information comes from:

- [Main design](technical-design.md): product purpose and roles.
- [Contracts](contracts.md): data ownership, identities, and expected handoffs.
- [Operations](operations.md): holds, resource pressure, and recovery.
- [Hooks](hooks.md): compaction refresh and handoff reminder diagnostics.
- [beads-ui][beads-ui]: the forked application, including its lit-html UI,
  Node/Express backend, Beads queries, and live-update connections.
- [Vite deployment guidance][vite]: built static assets and development serving.
- [Cloudflare Tunnel][tunnel] and [Access applications][access]: optional
  authenticated access to a localhost application.

[beads-ui]: https://github.com/mantoni/beads-ui
[vite]: https://vite.dev/guide/static-deploy.html
[tunnel]: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/
[access]: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/

## Application Foundation

Fork beads-ui and retain its lit-html rendering, issue details, search,
dependency and epic presentation, navigation helpers, Beads command adapter,
and live-update infrastructure. Add Fulcrum's fleet and project views and
apply its design system throughout the retained components. Existing upstream
components should save implementation work rather than become an embedded
second application with a different visual language.

Retain the Node/Express backend, including its supported Beads CLI access. Do
not reimplement those adapters in Python. Fulcrum's Python scripts remain
agent workflow and service helpers. Vite supplies development serving and
frontend builds, replacing upstream build wiring where necessary.

Write new dashboard code in TypeScript. Existing upstream checked JavaScript
can stay in place, with Prettier, ESLint, and its type checks retained. Preserve
upstream license notices and track the fork's base revision so useful fixes
can be incorporated deliberately.

The upstream application includes editing functionality. V1 removes its edit
controls and disables its mutating server handlers, including WebSocket
commands. Hiding controls alone is insufficient. Retain only registered brain
workspace access and read operations needed by Fulcrum.

## Information Architecture

There are three primary views: Status, Projects, and Newsfeed. A persistent
desktop sidebar provides navigation, the product identity, host connection
state, and a compact theme-following indicator.

Use the existing hash router for stable, bookmarkable views:

| Route | Contents |
| --- | --- |
| `/#/status` | Agent fleet, waits, scheduled work, recent completions |
| `/#/projects` | Project summaries, constraints, and future plans |
| `/#/newsfeed` | Fleet-wide bead cards |
| `/#/projects/:project_id/newsfeed` | Project-scoped bead cards |
| `/#/agents/:agent_id` | Read-only agent and assignment details |
| `/#/beads/:bead_id` | Issue contract, dependencies, and evidence |
| `/#/plans/:plan_id` | Approved plan and revision details |

Opening `/` redirects to `/#/status`. Invalid identities show a clear missing or
retained-history result, not a different similarly named item. Filter state and
search terms are represented in the hash route's query parameters so links
reproduce a view.

Navigation, filters, search, expansion, and copying identifiers are read-only
interactions. V1 has no assign, pause, promote, edit, or retry controls. An item
that needs intervention explains the condition and names the responsible role.

### Status

Status answers “who is responsible for what, and where does progress stand?”
Use summary counts followed by two-column agent cards grouped by activity.

Each card contains:

- Role emblem, readable task title, project, and role-number tag when present.
  Weavers keep their descriptive titles without a numbered tag.
- Current assignment or recurring job, with a link to its bead or plan.
- Reported workflow activity: working, waiting, paused, scheduled, or complete.
- Expected next actor and action for waits or escalations.
- Observed Codex runtime status, its timestamp, and availability separately.
- Current phase duration and relevant CI/resource state when known.
- Reported handoff problems and expected next action. Show recent reminders
  or hook errors in details when available.

Place action-required and inconsistent records first, then working agents,
healthy waits, scheduled jobs, and recent completions. Within each group, use
priority and stable assignment start time. A telemetry refresh must not
continuously reshuffle cards under the user's pointer.

For example, one card may say:

```text
Executor 18 · Battlement
Awaiting Overseer review · candidate C81
Reported 3 minutes ago · Next action: Overseer 18 reviews the candidate
Codex observed idle 10 seconds ago · Healthy recorded handoff
```

Agent details may show the last recorded context refresh, reminder, or hook
error. Handoff state comes from the agent's progress report, not a separate
receipt system. A past hook event does not prove the agent is currently running
or that a message was delivered. Keep routine diagnostics out of the newsfeed.

A disconnected adapter instead shows “Runtime status unavailable” and the last
observation time. It does not animate an idle task as running solely because
its last workflow phase was implementing.

Workflow improvements and architectural refactoring are core dashboard
content. Place a prominent recent-improvements section from `NEWS.md` near the
top of Status, showing what changed, why it matters, and the related work.
Include improvements to Fulcrum's own workflows and tools. Resource and
synchronization summaries link to read-only diagnostic details.

### Projects

Project cards combine the Archon's current summary with live work facts.
Narrative and derived facts retain separate timestamps.

- Project identity, integration health, and enabled state.
- Concise current situation and next intended direction from `NEWS.md`.
- Counts of active, ready, blocked, held, future, and recently completed beads.
- Activated plans, future plans, and reasons queued plans cannot start.
- Notable workflow improvements and architectural refactors for the project.
- A link to the project-specific newsfeed and approved planning documents.

Do not invoke a model to summarize the brain on each page load. The Archon owns
the narrative, and a parser extracts its structured Markdown. A stale summary
is labeled with its update time, not silently rewritten from issue titles.

### Newsfeed and details

Newsfeed is task-based: each card represents one bead and its current state.
The default includes all nonclosed work and work closed during the last seven
days, with a visible control to include older completed work.

Give workflow improvements and architectural refactors a prominent featured
section above the task cards, drawn from `NEWS.md`. Show their outcomes and
benefits with links to the underlying tasks. Project feeds show the same
content scoped to their project; these updates are not buried in diagnostics.

Provide status, project, plan, role/assignee, and priority filters, plus text
search. Default ordering places active and action-required work first, then
ready, held/future, and recently completed work. Within a group, sort by most
recent meaningful change with bead ID as a stable tie breaker.

Each bead card displays title, project, plan when present, priority, current
status, assigned pair, a short outcome summary, and the most important blocker
or completion fact. Opening it reveals:

- The full task contract and linked approved plan revision.
- Dependencies and the explicit reason the task is or is not eligible.
- Assignment phase and links to the relevant Codex handoffs.
- Candidate/source identity and links to retained review or CI evidence.
- Completion, pending source push, or cleanup obligations when applicable.

The project-specific feed uses the same components and ordering with its
project filter fixed. A project without work has an informative empty state,
not invented example tasks.

## NEWS.md Contract

`NEWS.md` contains a current project-summary map and a rolling sequence of
human-readable updates maintained by the Archon. Edit, commit, and push it as
ordinary Markdown. The dashboard reads the working file and refreshes when it
changes; no separate publishing step is required.

The frontmatter map supplies current summaries keyed by project ID:

```yaml
project_summaries:
  fulcrum:
    updated_at: 2026-09-11T18:00:00Z
    current: Review handoffs now carry the evidence reviewers need.
    next: Measure review latency and address repeated CI stalls.
```

Below the frontmatter, use dated headings and small metadata fields so the
reader can identify project scope and prominently feature improvements:

```markdown
## 2026-09-11 | Shorter review handoffs
Projects: fulcrum
Category: workflow
Beads: fc-91

Executors now attach validation evidence with their review request.
This removes a repeated evidence-request round trip; latency measurement
is still pending.
```

Categories are `workflow`, `architecture`, and `progress`. Feature workflow
and architecture entries prominently on Status and Newsfeed, including on
project-scoped views. An entry may name several projects. Bead references link
to the issue details. Current summaries link future plans using normal Markdown
links; no model runs on page load to infer missing narrative.

Explain the problem addressed, what changed, and the benefit, distinguishing
measured gains from expectations. Also cover meaningful outcomes, scope
changes, incidents, and recoveries. Updates do not mirror every tool call.
Read existing entries before adding one, and prune superseded narrative to
keep the file brief; Git preserves its history. Keep unresolved incidents
visible while they remain relevant.

Invalid structure produces a parser diagnostic and the previous valid view.
Do not guess the wrong project's summary. Reuse the fork's Markdown rendering
and sanitization, keeping executable HTML disabled and links safe.

## Visual Foundations

The aesthetic comes from disciplined composition and role symbolism. Avoid
decoration behind body text, overwhelming glow, or effects that obscure state.
Use violet for identity and navigation, gold for important current attention,
and distinct semantic status colors.

### Color and theme

Follow `prefers-color-scheme` automatically and update when the OS changes.
Both themes are fully designed; light mode is not merely inverted dark mode.

| Token | Dark | Light |
| --- | --- | --- |
| Canvas | `#10101A` | `#F6F3FC` |
| Card surface | `#181826` | `#FFFFFF` |
| Raised surface | `#222235` | `#EEE8F7` |
| Primary text | `#F2F0FA` | `#251C36` |
| Secondary text | `#A6A4B8` | `#625C73` |
| Violet accent | `#C4B5FD` | `#6D28D9` |
| Gold accent | `#F3C969` | `#8C5B00` |
| Success text/icon | `#6EE7B7` | `#166534` |
| Failure text/icon | `#FDA4AF` | `#9F1239` |
| Information text/icon | `#93C5FD` | `#1E40AF` |

Use accent colors for text/icons on the card surface or canvas. Filled accent
controls require a separately verified foreground, not automatic white text.
Text must meet 4.5:1 contrast at ordinary sizes; essential UI boundaries and
focus indicators meet 3:1. Status always includes text or a distinct symbol.

Card borders may be subtle decoration, but a focused or interactive boundary
must be unambiguous. Focus uses a solid two-pixel violet outline with a
two-pixel offset. Gold is not the sole indicator of urgency or selection.

### Typography and spacing

Use a system sans-serif body stack and system monospace only for identifiers,
timestamps where alignment helps, and compact diagnostics. Avoid monospace
paragraphs or ornamental lettering for task descriptions.

- Base text: 16px with 1.5 line height.
- Secondary metadata: 14px minimum with 1.4 line height.
- Card title: 18px, medium or semibold; page title: 28px, semibold.
- Spacing scale: 4, 8, 12, 16, 24, 32, and 48px.
- Card padding: 24px desktop, 16px mobile; card gap: 24px desktop, 16px mobile.
- Corners: 16px cards and 10px compact controls, with restrained shadows.
- Text-heavy detail views use a readable maximum prose width of 72 characters.

Identifiers can wrap or truncate with a copy affordance. Task titles wrap to
multiple lines; critical state and blocker information must not disappear into
an ellipsis or a hover-only tooltip.

### Role symbols and composition

Give each role a small, consistent vector emblem built within the UI asset
system. Emblems accompany text labels and never become the only identification.

Use a crown for Archon, interlaced strands for Weaver, an eye for Overseer, a
hammer for Executor, a lantern for Night Watchman, a book for Sage, and a prism
for Inquisitor. Keep a shared stroke weight and optical size. Their colors come
from the design tokens rather than seven competing palettes.

Atmosphere comes from a faint violet/gold gradient near page edges, a restrained
emblem treatment, and subtle active-state highlights. Decorative layers remain
outside text backgrounds and are excluded from accessible names.

### Layout and mobile

At widths of 1024px and above, use a 224px sidebar and a two-column card grid.
At 768–1023px, compact the navigation to a top bar and retain two columns only
when each card has at least 320px of available width. Below that, use one
column.
At widths below 768px, use compact top navigation with an accessible menu.

- Maximum main content width: 1440px, centered within the available area.
- Read cards in DOM row order, left to right and then downward. Do not use
  masonry that changes the reading sequence.
- Touch targets are at least 44px; filters wrap rather than causing page-wide
  horizontal scrolling.
- Details use a full page on mobile. Desktop may use a detail panel while
  keeping the canonical route and browser back behavior correct.
- Verify 320px-wide layouts, 200% zoom, long titles, and missing metadata.

## Motion

Motion explains continuity and activity. Use Material-style easing with
consistent durations, rather than unrelated component-library defaults.

```css
:root {
  --motion-fast: 120ms;
  --motion-standard: 200ms;
  --motion-enter: 280ms;
  --ease-standard: cubic-bezier(0.2, 0, 0, 1);
}
```

- Navigation and detail panels use opacity plus at most 8px of translation.
- Expand/collapse transitions take 200ms and preserve keyboard focus.
- A changed status receives one short highlight, not repeated flashing.
- Verified running agents use a slow rotating emblem ring or moving border
  segment with a 2.4-second cycle. Use transform/opacity, not costly layout
  work.
- A reported working phase without runtime confirmation gets a clearly labeled
  unverified state; its indicator must not imply observed execution.
- Healthy waiting uses a static waiting symbol. It must not resemble a build
  spinner. Scheduled work shows its due time.
- Off-screen and background-tab animations pause to reduce host load.
- `prefers-reduced-motion` removes continuous and spatial animation, retaining
  static labels/icons and immediate state changes.

Limit persistent animation to the activity indicators actually visible. The
dashboard should not materially compete with the builds it is monitoring.

## Data and Live Updates

Reuse beads-ui's Beads command adapter and read-only HTTP/WebSocket operations
for issue data. Its existing database watchers and subscriptions provide the
starting point for live updates. Verify those notifications against the chosen
local Dolt server: directory recognition alone does not prove that every
server-side database edit triggers a refresh.

If the watcher misses Dolt changes, refresh subscribed issue queries on a
shared five-second backend timer. Coalesce identical reads so browser tabs do
not each launch their own Beads commands. The fallback should be a small
adapter change, not a new issue export or replication system.

Add a read-only endpoint for Fulcrum-specific data: project registrations,
agent reports, assignments, holds, Markdown plans and NEWS, and available
runtime and hook observations. The frontend joins these with existing Beads
results by project, plan, bead, and actual Codex task IDs. Only expose
registered files and fields needed by the UI; never serve the raw
application-data directory.

Each source includes its update or observation time and availability. For
example, an unavailable Codex runtime source reports:

```json
{
  "source": "codex_runtime",
  "availability": "unavailable",
  "last_observed_at": "2026-09-11T18:00:00Z",
  "reason": "No compatible existing app-server connection"
}
```

Read current Markdown and atomically replaced local JSON. Refresh visible
Fulcrum data every five seconds, pause browser polling in background tabs, and
refresh on return. Changes should normally appear within ten seconds. A
small shared backend cache keeps tool invocations proportionate.

Data sources need not describe the same instant. A plan can appear before its
beads, and an agent can report completion before NEWS is updated. Display
available content with its timestamps. Missing linked tasks show “Tasks being
prepared” when that is known; unknown references show a diagnostic. Do not
construct a publication manifest or retain versioned dashboard snapshots.

Reuse upstream list pagination and filtering where possible. Preserve filter
and scroll state during refresh and avoid gratuitous card movement. Filter by
current assignment, or the last assignment for completed beads; use actual
task IDs rather than titles for assignee matching.

On read failure, show cached content with its age and error. Initial failures
show a retryable error, while a successful empty query shows an empty state.
Display known pending Git and Beads pushes separately from unavailable runtime
observations. Remote push status does not hide current local work. Optional
hook diagnostics are informational; their absence does not hide agent state
or prove a handoff failed.

Enforce read-only behavior on the server:

- Reject issue creation, updates, deletion, and other workflow mutations over
  both HTTP and WebSocket, including handlers retained from upstream.
- Allow only configured workspace access, subscriptions, and read queries.
- Do not accept arbitrary filesystem paths, shell commands, SQL, or Codex RPC
  method names. Evidence links open curated references, not a file browser.
- Keep assignment, pause, promotion, and agent messaging outside this API.

## Serving and Secure Remote Viewing

Vite builds the lit-html application; the forked Node/Express backend serves
those assets, read-only data, and WebSocket subscriptions from the same
origin. Vite's dev server is for development, and its preview command is not
the deployed persistent server.
[Vite's deployment guide][vite] distinguishes these uses.

The Archon manages service start, health inspection, and targeted restart
through scripts. macOS service management retains the backend across Codex
turns and restarts it after failure. Health responses expose the served version
and source availability without leaking credentials or private document text.

Optional remote access uses a named Cloudflare Tunnel to the loopback backend
and a Cloudflare Access application restricting access to the owner's configured
identity. The local connector establishes outbound connections; a publicly
routable origin address is not required. [Cloudflare Tunnel][tunnel]

- Remote access is disabled until hostname, owner identity, and Access policy
  are configured. There is no public anonymous dashboard fallback.
- Protect HTML, every API route, and WebSocket upgrades with the same Access
  policy. The live-update channel must not bypass authentication.
- Validate Access tokens on requests arriving through the configured remote
  hostname, including issuer, audience, signature, and expiration.
- Keep explicit allowed hosts and origins. Localhost access remains available
  locally; a remote request cannot claim the local bypass by arbitrary headers.
- Configure the tunnel's origin host handling to preserve the protected remote
  hostname. Test that missing/invalid tokens fail at the origin for that host.
- Keep tunnel credentials and identity-provider secrets outside tracked data.
- Bind Dolt, Codex transports, development servers, and administrative commands
  outside the exposed dashboard route. The tunnel exposes none of them.
- Use private/no-store caching for API and authenticated document responses.
  Versioned public frontend assets may use ordinary immutable asset caching.

Remote viewing reflects the local Mac's availability. If it is asleep or the
connector is disconnected, report unavailability; do not imply that a separate
cloud copy of the agent fleet continues running.
