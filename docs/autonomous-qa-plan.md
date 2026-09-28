# Autonomous QA — Project Plan

A framework that runs QA on real finished (and half-finished) software, finds UI and functional bugs, packages evidence, and optionally opens fix PRs and retests until green.

**Beachhead:** macOS apps. **Next:** iOS Simulator. **Scale-out (v1):** local workers **and** a **Tart VM adapter** (isolated GUI workers on Apple Silicon). **After v1:** paid cloud Mac runners behind the same pluggable interface. **Later target surfaces:** web/web apps (named only; not designed here).

**Architecture stance:** Assemble existing drivers and tools; build custom glue for planning, **parallel orchestration**, reporting, and the PR retest loop. Do not invent a new agent runtime unless a chunk truly requires it. One “swarm pass” is a **fleet of workers**, not a single serial run.

**PR modes (config):**
1. **Human-in-the-loop (default)** — file findings + draft PR (+ optional tracker issues), then stop for a human.
2. **Autonomous** — open PR, fix, retest, iterate until green (with a loop safety cap).

**Parallelism (config):** User sets how hard to push testing — backend (`local` | `vm` | `cloud`), max concurrent workers (**default `workers = 2`**), wall-time / worker-minute budgets, and a **cloud spend cap** (currency ceiling users dial up or down). **v1 backends:** `local` + `vm` via **Tart**. `cloud` interface may stub/meter early; a real paid Mac host is post-v1. The orchestrator shards intents (and exploratory seeds) across workers, aggregates results, then optionally enters the PR loop once.

**Locked defaults (from scoping):**
- `pr.mode = human`
- `workers = 2` (local default)
- Video: **always record** by default; config toggle for `always` | `on_failure` | `exploratory_only`
- Issues: create GitHub and/or Linear issues from findings; users can supply a **custom issue format/template** the reporter fills

---

## 1. User perspective — how it works

### What you install and run

You install Autonomous QA as a local CLI (or invoke it from Cursor / Claude Code / Codex / similar agent CLIs). It does not replace your build system. It wraps drivers you already use or can install (Accessibility APIs, XCUITest runners, screenshot tools, git/gh), plus **runner backends** for local processes, VMs, or cloud machines.

Typical first run (single worker — good for smoke):

```bash
aqa init                    # creates aqa.config.toml + report folder conventions
aqa run --app /path/to/MyApp.app --intent intents/smoke.md
```

Typical scale-out run (test as much as you allow):

```bash
aqa run --app /path/to/MyApp.app \
  --intent intents/ \
  --backend cloud \
  --workers 12 \
  --max-wall-time 2h \
  --max-spend 10.00 \
  --spend-currency USD
```

Or from an agent chat: “Run Autonomous QA against MyApp with the full intent folder; cloud backend; 12 workers; max spend $10; human-in-the-loop.”

### Pointing it at a macOS app

You give it a path to a built `.app` (or a build command that produces one). Optional: bundle ID, launch args, env vars, and whether the app is a half-wired prototype (more exploratory bias) vs nearly shipped (stricter pass/fail).

Each **worker** launches its own app session in an isolated environment (separate process at minimum; preferably a separate VM/cloud instance for true GUI parallelism), attaches an accessibility/UI driver, and starts a session log + **screen recording** (default: always on; toggle to record only on failure or only during exploratory hunting).

### Giving it intent (mix and match)

You can combine any of:

| Input | What you provide | What the runner does |
| --- | --- | --- |
| **Natural language** | A short markdown or chat prompt (“Sign in, open Settings, toggle Dark Mode, confirm the window title updates”) | Planner turns this into steps; Explorer fills gaps when the UI doesn’t match |
| **Recorded flows (JSON)** | A `.json` file of steps (click/type/…) in our schema — see below | Replays with soft matching; fails or explores when controls moved |
| **Existing suites** | XCUITest / other suite path or command | Runs as scripted pass/fail; failures feed the Reporter and optional Fix loop |

Same campaign can mix them: e.g. suite for regression + NL for a new feature + exploratory pass for “anything else broken near Settings.” The orchestrator **shards** this work across workers (by intent file, suite partition, or exploratory seed) so many paths run at once.

#### Two different “recordings” (do not confuse)

| Kind | What it is | How you get / access it | Where it shows up on a bug |
| --- | --- | --- | --- |
| **Recorded flow (JSON intent)** | Steps you want the tool to **replay as a test** | You (or an agent) write/edit a JSON file under e.g. `intents/`; or run **`aqa record`** which watches you drive the app once and **writes that JSON** into your project so you can open it in the editor, commit it, and re-run later | On failure: a **replay stub** (path or inline steps) is attached so you can re-run the same flow; not a substitute for video |
| **Session / failure video** | Screen capture of the **run that found the bug** (default: always recording) | Written under the campaign report `media/`; linked from the finding | **Yes — attached to the finding, issue, and draft PR** (upload or link per tracker limits); plus screenshots + human steps |

So: JSON is how you *author* a flow to test. Video is what you *watch* when something breaks. Both can appear on the issue/PR; the video is the primary “show me the bug” artifact.

### Running many tests at once (fleet, not one pass)

Autonomous QA is built to **keep testing until your caps say stop**, not to fire a single serial swarm and quit.

You configure:

| Knob | Meaning |
| --- | --- |
| **Backend** | `local` — workers on this machine; `vm` — each worker in a VM; `cloud` — each worker on a cloud runner (e.g. Mac cloud hosts) |
| **Workers** | Max concurrent sessions (**default 2** on local; raise for cloud) |
| **Campaign budget** | Max wall time, max total worker-minutes |
| **Cloud spend cap** | Hard currency ceiling for the campaign (`max_spend` + currency). **Required when `backend = cloud`**. Users treat it as a dial: raise it to test more, lower it to stay cheap. Hit the cap → stop scheduling new shards; in-flight workers finish or cancel per config |
| **Shard strategy** | By intent file, by suite class, by exploratory seed/region |

**What that feels like:** You start one campaign. A dashboard-style live view (terminal or agent transcript) shows worker 1…N: which intent each has, pass/fail/finding counts, who is idle vs busy, and **estimated spend used / remaining vs your cap**. When a worker finishes a shard, it pulls the next until the queue or a budget/spend cap is exhausted. You get **one merged report** for the campaign, not N disconnected folders you have to stitch by hand.

**Honest constraint (macOS GUI):** Multiple UI drivers fighting one display/session is unreliable. Local backend is fine for a small worker count and for headless/suite shards; for “as many as needed” GUI sessions, prefer **vm** or **cloud** so each worker owns an isolated Mac environment. The product recommends this in config comments and docs.

### What you see during a run

Live stream (terminal or agent transcript):

- Campaign progress: queue depth, active workers, backend
- **Spend meter** (cloud): estimated spent / `max_spend`, currency, and “approaching cap” warnings
- Per worker: mode (scripted / exploratory / visual), current goal/step
- Passes, soft mismatches, hard failures
- When exploratory: hypotheses (“Settings gear missing — trying menu bar”)
- Artifact paths as they are written (screenshot, clip, ticket draft)

Campaigns stay interruptible. Stopping mid-campaign still keeps partial artifacts and merges whatever workers completed.

### What you get when it finishes

Per finding (as fits the bug):

- **Failure video** (or short clip of the failing path) — **included on the issue / draft PR** (file attach or hosted link)
- **Screenshot(s)** with highlighted control/region when useful
- **Steps to reproduce** (human-readable) **+ JSON replay stub** (so the same flow can be re-run)
- **Ticket draft** (title, severity guess, environment, build id, **worker/backend** that caught it)

Plus a **campaign report**: summary across all workers, pass/fail matrix for scripted items, exploratory findings (deduped when clearly the same bug), visual-diff deltas vs baseline (if configured), coverage stats (intents run / skipped / budget- or spend-stopped), and **spend summary** (cap, estimated spent, currency, stop reason if capped).

### How the two PR modes feel

**Human-in-the-loop (default)**  
Campaign ends with merged findings + optional **draft PR** (branch, failing test note, suggested fix sketch) and **optional GitHub/Linear issues** rendered from your issue template. Nothing merges. You review, edit, and decide.

**Autonomous**  
Same merged findings, then the Fix loop: branch → propose fix → open PR → **retest failing intents** (again fan-out across workers if configured) → iterate until green or hit the **loop safety cap** (config: max iterations / max PRs / max wall time). Caps always win; leftover failures stay as open findings.

---

## 2. Product / system specs (lightweight SpecDD)

Specs are checkable acceptance criteria. Grouped by feature area.

### 2.1 App Driver (macOS first)

**Purpose:** Launch, observe, and drive a real macOS `.app` so scripted and exploratory agents can act on UI — once per worker session.

**User-visible behavior:** User points at an app path or build command; each worker launches, focuses, clicks/types/scrolls via accessibility (and fallbacks where needed), captures screenshots and optional video.

**Acceptance criteria:**
- [ ] Launch `.app` from path; report clear error if missing/unsigned/crash-on-launch
- [ ] Query accessibility tree for windows, buttons, text fields, menus
- [ ] Perform click, type, keychord, scroll, menu select; wait-for-element with timeout
- [ ] Capture full-window screenshot and optional session video
- [ ] Attach build metadata (path, bundle id, version string if present) to the worker run
- [ ] Survive app relaunch mid-run when the planner requests it
- [ ] Support **isolated session** assumption: driver does not require exclusive ownership of the host beyond its assigned environment (so orchestrator can place it in a VM/cloud worker)

**Non-goals:** Deep binary instrumentation; reverse engineering private APIs; Windows/Linux desktop as *targets* (later).

**Dependencies:** macOS environment (host, VM, or cloud); Accessibility permission; existing screenshot/video tooling; optional XCUITest bridge later.

---

### 2.2 Intent Ingestion

**Purpose:** Accept NL, recorded flows, and existing suites in one campaign config; produce a **shardable work queue**.

**User-visible behavior:** User drops intents into config or CLI flags; runner normalizes them into a plan with tags (scripted / exploratory / visual) and shard units the orchestrator can distribute.

**Acceptance criteria:**
- [ ] Parse NL intent files (markdown) into ordered goals + optional constraints
- [ ] Import **v1 recorded flows as a simple JSON action list** we define (document schema in config/docs); leave other-recorder importers for later
- [ ] Provide **`aqa record`**: user (or agent) drives the macOS app once; tool writes a JSON flow file into the project (path printed; file openable/editable/committable)
- [ ] Allow hand-authored or agent-authored JSON without using `aqa record`
- [ ] Invoke an existing suite via shell command or known runner (e.g. `xcodebuild test` / XCUITest) and map exit + logs into pass/fail items
- [ ] Allow mixing all three in one `aqa run` campaign
- [ ] Emit a work queue of shard units (one intent file, suite partition, or exploratory seed each)
- [ ] Reject empty intent sets with a clear message

**Non-goals:** A polished GUI recorder product in MVP (`aqa record` CLI is enough); rewriting XCUITest into another language.

**Dependencies:** Planner; App Driver; suite runners available inside each worker environment when suites are used.

---

### 2.3 Explorer (scripted + exploratory)

**Purpose:** Execute scripted steps for pass/fail; hunt bugs when intent is loose or UI diverges — inside a single worker.

**User-visible behavior:** Scripted mode shows green/red per step. Exploratory mode narrates attempts and logs findings when it hits crashes, dead ends, wrong state, or broken affordances.

**Acceptance criteria:**
- [ ] **Scripted:** each step has explicit pass/fail; failure stops that chain (or continues per config) and records evidence
- [ ] **Exploratory:** given a goal + scope (e.g. “Settings area”), attempt multiple strategies before declaring blocked
- [ ] Detect crash-on-action, unresponsive UI (timeout), and obvious empty/error states
- [ ] Work on half-wired prototypes: missing controls become findings, not silent skips
- [ ] Emit structured finding objects the Reporter can consume
- [ ] Honor per-worker step/time budgets from the orchestrator

**Non-goals:** Formal model-based testing theory; infinite crawl of every control (bound by time/step budget).

**Dependencies:** App Driver; Intent Ingestion; budgets in Config / Orchestrator.

---

### 2.4 Visual Diff

**Purpose:** Catch unintended visual changes against baselines.

**User-visible behavior:** User sets baseline screenshots (or “promote current”). Workers produce diffs with highlighted regions and a pass/fail (or warn) per baseline; campaign report merges them.

**Acceptance criteria:**
- [ ] Capture named screenshots at planner checkpoints
- [ ] Compare to baseline with configurable threshold; emit diff image + score
- [ ] Support “update baselines” as an explicit command (not silent overwrite on fail)
- [ ] Tag visual failures separately from functional failures in the report
- [ ] Concurrent workers reading the same baseline set do not corrupt baselines (writes only via explicit update command)

**Non-goals:** Pixel-perfect cross-device matrix in MVP; design-tool Figma sync.

**Dependencies:** App Driver screenshots; baseline store on disk (git-friendly); Orchestrator merge.

---

### 2.5 Reporter

**Purpose:** Turn raw worker data into campaign artifacts humans and agents can act on.

**User-visible behavior:** After a campaign, a report folder contains per-worker media plus a merged summary: videos/clips, screenshots, steps, ticket drafts, coverage, and which backend/worker caught each issue.

**Acceptance criteria:**
- [ ] Campaign directory with stable layout (`summary.md`, `workers/<id>/`, `findings/`, `media/`, `raw/`)
- [ ] Per finding: title, severity guess, steps, screenshot and/or video when available, environment/build, worker id, backend, path to **JSON replay stub** when the failing shard had a recorded flow (or a generated stub from the executed steps)
- [ ] Ticket body rendered from a **user-supplied issue format/template** (markdown with placeholders for title, steps, video link/path, screenshot paths, replay JSON path, etc.); fall back to a built-in default template if none provided
- [ ] Optional **create issue** adapters for **GitHub** and **Linear** (config: enable per tracker + credentials by reference); still always write local ticket files
- [ ] When creating GitHub/Linear issues or draft PRs: **attach or link the failure video** (and screenshots); include replay JSON path or fenced stub in the body per template / tracker attachment limits
- [ ] Scripted matrix: intent → pass/fail → link to evidence (across shards)
- [ ] Best-effort **dedup** of clearly identical findings from parallel workers (same title+fingerprint; keep all evidence links) so parallel workers do not spam duplicate issues
- [ ] Partial campaigns still produce a usable merged report
- [ ] Coverage section: shards completed / failed / cancelled / not started due to budget or spend cap
- [ ] Spend section (when cloud/vm metered): `max_spend`, currency, estimated spent, stop reason if spend-capped
- [ ] Video policy honored: `always` (default) | `on_failure` | `exploratory_only`

**Non-goals:** Full hosted SaaS dashboard in MVP (live terminal view is enough); supporting every tracker on day one (GitHub + Linear first; template keeps format portable).

**Dependencies:** Explorer; Visual Diff; media from App Driver; Orchestrator completion events; `gh` / Linear API tokens when issue creation is enabled.

---

### 2.6 Fix + PR Loop

**Purpose:** Optionally turn **merged** campaign findings into PRs and retest (with optional parallel retest).

**User-visible behavior:** Config selects **autonomous** or **human-in-the-loop**. HITL stops after draft PR / findings. Autonomous iterates fix → PR → retest until green or safety cap. Retest may reuse the same worker fleet settings.

**Acceptance criteria:**
- [ ] Config flag `pr.mode = autonomous | human` (names may vary; behavior must match); **default `human`**
- [ ] **HITL:** create branch + draft PR body from merged findings; do not push fixes beyond draft; exit for human
- [ ] **Autonomous:** implement fix attempts via the hosting agent CLI; open/update PR; re-run failing intents (fan-out allowed); repeat
- [ ] Safety cap: max iterations **and** max wall time **and** max PR updates (all configurable; defaults documented)
- [ ] Cap hit → leave PR open with remaining failures listed; never silent infinite loop
- [ ] Retest uses the same intent set that failed (not an unrelated full suite unless configured)
- [ ] Only **one** fix/PR loop runs per campaign (workers do not each open competing PRs)

**Non-goals:** Auto-merge to main; production deploy; multi-repo monorepo magic in MVP.

**Dependencies:** Reporter (merged); git + `gh` (or equivalent); agent CLI for code edits; Orchestrator for retest fan-out.

---

### 2.7 Config / Modes

**Purpose:** One place for app target, intents, coverage mix, budgets, PR mode, baselines, **and parallelism / backend**.

**User-visible behavior:** `aqa init` writes a commented config; overrides via CLI flags. Users dial concurrency **and cloud spend** up or down without changing intents.

**Acceptance criteria:**
- [ ] Config covers: app path/build command, intents, coverage toggles (scripted / exploratory / visual), per-worker budgets (time, steps), **campaign budgets** (wall time, worker-minutes), PR mode + caps (**default `human`**), baseline path
- [ ] Config covers parallelism: `backend = local | vm | cloud`, `workers` (max concurrent, **default 2**), shard strategy, backend credentials/endpoints by reference (env/secret names — not plaintext in repo)
- [ ] Config covers **cloud spend cap** as a first-class knob: `max_spend` (number) + `spend_currency` (e.g. `USD`); required whenever `backend = cloud` (fail fast if missing or ≤ 0)
- [ ] `max_spend` also supported for metered `vm` backends when the adapter reports cost; ignored (with a clear note) on pure `local`
- [ ] Config covers **video.mode** = `always` (default) | `on_failure` | `exploratory_only`
- [ ] Config covers **issues**: enable GitHub and/or Linear; path to user issue template; credentials by reference
- [ ] CLI flags override config for one-off campaigns (`--workers`, `--backend`, `--max-wall-time`, `--max-spend`, `--spend-currency`, `--video-mode`, `--pr-mode`)
- [ ] Invalid config fails fast with field-level errors
- [ ] Prototype vs shipped “maturity hint” adjusts Explorer aggressiveness (documented)
- [ ] Documented defaults: `workers = 2` on `local`; `pr.mode = human`; `video.mode = always`; sample/default cloud `max_spend = 10` with `spend_currency = USD`; warn when GUI parallelism on local exceeds a safe threshold

**Non-goals:** Remote multi-tenant config service as a product requirement; guaranteeing provider billing accuracy to the cent (use best-effort estimates from the adapter + stop before exceeding cap).

**Dependencies:** None (foundation); Orchestrator consumes these fields.

---

### 2.8 CLI / Agent entrypoints

**Purpose:** Same core runnable from shell or from Cursor / Claude Code / Codex / similar — for single runs or fleets.

**User-visible behavior:** `aqa run|report|baseline|init|status` locally; agent prompts can invoke the same commands. `status` shows live campaign/worker state.

**Acceptance criteria:**
- [ ] CLI supports `init`, `run`, `record` (write JSON flow), `report` (re-print last summary), `baseline update`, `status` (active campaign)
- [ ] Non-zero exit if any scripted failure or unhandled crash (configurable warn-only for exploratory-only campaigns)
- [ ] Machine-readable campaign summary JSON alongside markdown
- [ ] Documented prompt snippets for agent CLIs (“how to invoke Autonomous QA” including `--workers` / backend / `--max-spend`)
- [ ] No hard requirement on a single cloud vendor or model

**Non-goals:** Building a proprietary agent OS; locking to one model provider or one cloud host.

**Dependencies:** All feature areas; assemble-first glue.

---

### 2.9 Run Orchestrator (parallel fleet)

**Purpose:** Turn a campaign into many concurrent worker sessions across local / VM / cloud backends, then merge results. This is the difference between “run the swarm once” and “test as much as the user allows.”

**User-visible behavior:** User sets backend + worker count + time budgets + **spend cap**. Orchestrator builds a shard queue, starts up to N workers, feeds them work until the queue is empty or a budget/spend cap hits, streams progress (including spend meter), and hands merged artifacts to Reporter / PR loop.

**Acceptance criteria:**
- [ ] Accept a shardable work queue from Intent Ingestion
- [ ] Run up to `workers` concurrent sessions on the selected backend
- [ ] **local** backend: spawn worker processes/sessions on the host with a documented concurrency ceiling for GUI vs suite-only shards
- [ ] **vm** backend: provision or reuse VMs (pluggable provider adapter), place one worker per VM (or per isolated session), tear down or recycle per config
- [ ] **cloud** backend: dispatch workers to a cloud runner adapter (pluggable; e.g. Mac cloud hosts); no hard-coded single vendor in core
- [ ] Respect campaign budgets: max wall time, max worker-minutes — stop scheduling new shards when hit; allow in-flight workers to finish or cancel per config
- [ ] Enforce **`max_spend` cloud spend cap**: adapters report estimated cost per worker-minute (or equivalent); orchestrator refuses to start cloud campaigns without a positive cap; stops scheduling new shards when estimated spent + next shard would exceed cap; records stop reason `spend_cap`
- [ ] Configurable overrun policy for in-flight workers at spend cap: `drain` (default — let current shards finish) or `cancel` (tear down early to minimize overrun)
- [ ] Live progress: active workers, queue depth, shard outcomes, spend used/remaining
- [ ] Crash/isolation: one worker failure does not kill the campaign; shard marked failed; worker slot freed
- [ ] Artifact collection from each worker into the campaign layout
- [ ] Idempotent enough to resume a cancelled campaign’s remaining shards (MVP: “rerun failed/pending” command acceptable) without resetting spend accounting for the campaign unless user opts in
- [ ] Same worker entrypoint binary/script used on every backend (only environment bootstrap differs)

**Non-goals:** Building your own hypervisor or cloud from scratch; guaranteeing infinite scale without user-supplied VM/cloud capacity; designing web-app target drivers here; perfect real-time provider invoices (best-effort estimates that bias toward not exceeding the user cap).

**Dependencies:** Config; Intent Ingestion; worker pipeline (Driver + Explorer + Visual + local Reporter slice); provider adapters for vm/cloud (assemble existing tools).

---

## 3. Work chunks

Ordered for delivery. **MVP / v1** = first useful macOS product slice. Sequence: single-worker local → local fleet → **Tart VM adapter (in v1)** → paid cloud later.

| ID | Chunk | Phase | MVP / v1? |
| --- | --- | --- | --- |
| C1 | Config, CLI skeleton, report layout (incl. parallelism fields) | Foundation | **Yes** |
| C2 | macOS App Driver (launch + a11y act + screenshot/video) | macOS | **Yes** |
| C3 | Intent Ingestion (NL + suite + JSON record) → shardable queue | macOS | **Yes** |
| C4 | Scripted Explorer + basic Reporter artifacts (single worker) | macOS | **Yes** |
| C5 | Exploratory hunting + budgets | macOS solid | Stretch; **Yes** if time |
| C6 | Visual Diff + baselines | macOS solid | After core path |
| C7 | Fix + PR Loop (HITL first, then autonomous + caps) | macOS solid | HITL in v1; autonomous next |
| C8 | **Run Orchestrator — local parallel workers + campaign merge** | Scale | **Yes** (after C4) |
| C9a | **Tart VM adapter** (`backend = vm`) | Scale | **Yes — in v1** (after C8; does not block C1–C4) |
| C9b | Cloud adapter interface + spend metering; paid Mac host later | Scale | Interface/spend in v1 if cheap; **real cloud host post-v1** |
| C10 | iOS Simulator driver + intent parity (+ fleet-aware) | iOS | No |
| C11 | Later target surfaces (web) | Later | No |

### C1 — Config, CLI, report layout **(MVP)**

- **Scope:** `aqa init/run/report/status` stubs; config schema including `backend`, `workers`, campaign budgets, **`max_spend` / `spend_currency`**; empty campaign directory layout; JSON + markdown summary shells.
- **Specs:** 2.7, parts of 2.8 and 2.5 (layout only).
- **Dependencies:** None.
- **Done when:** User can `init`, run a no-op campaign, get a valid empty report folder and exit codes; parallelism and spend-cap knobs parse and validate (cloud without `max_spend` fails fast) even if only 1 worker is wired.

### C2 — macOS App Driver **(MVP)**

- **Scope:** Launch `.app`, a11y query/act, screenshot, optional video, build metadata; session isolation assumptions documented.
- **Specs:** 2.1.
- **Dependencies:** C1.
- **Done when:** Scripted “click this button by label” demo against a sample macOS app works reliably with Accessibility permission documented.

### C3 — Intent Ingestion **(MVP)**

- **Scope:** NL markdown intents; shell-invoked suite pass-through; **JSON recorded-flow** import (owned schema) + **`aqa record`** to produce that JSON; emit shardable work queue.
- **Specs:** 2.2.
- **Dependencies:** C1; C2 for end-to-end.
- **Done when:** One campaign config mixes NL + suite command; both appear as separate shard units in the plan/queue.

### C4 — Scripted Explorer + Reporter **(MVP)**

- **Scope:** Execute scripted steps in one worker; on fail, capture screenshot/video clip + steps + ticket draft; summary matrix.
- **Specs:** 2.3 (scripted), 2.5 (single-worker subset).
- **Dependencies:** C2, C3.
- **Done when:** Deliberate bug in a sample app produces a complete finding artifact pack.

### C5 — Exploratory hunting

- **Scope:** Goal-directed exploration with step/time budgets; findings for crashes, blocks, broken UI; prototype maturity hint; exploratory seeds as shard units.
- **Specs:** rest of 2.3.
- **Dependencies:** C4.
- **Done when:** Loose NL goal on a half-wired build yields ≥1 high-quality finding without a full scripted script.

### C6 — Visual Diff

- **Scope:** Baselines, compare, diff images, update command; safe concurrent reads.
- **Specs:** 2.4.
- **Dependencies:** C2, C1.
- **Done when:** Changing a button label/color fails visual check with a reviewable diff; `baseline update` promotes.

### C7 — Fix + PR Loop

- **Scope:** HITL draft PR first from **merged** findings; then autonomous loop with triple safety cap; retest failed intents (orchestrator fan-out when C8+ available).
- **Specs:** 2.6; remaining 2.8 agent docs.
- **Dependencies:** C4 (findings); C8 for parallel retest; git/gh; agent CLI available in env.
- **Done when:** HITL demo opens a draft PR from a finding; autonomous demo fixes a seeded bug and goes green within cap (or stops cleanly at cap); never opens one PR per worker.

### C8 — Run Orchestrator (local parallel) **(MVP for multi-test)**

- **Scope:** Shard queue → N local workers → progress → merge artifacts/findings; campaign budgets; spend-cap config plumbing (no-op cost on local); `aqa status`; safe GUI concurrency warnings.
- **Specs:** 2.9 (local path), 2.5 (merge/dedup), parallelism parts of 2.7–2.8.
- **Dependencies:** C1, C3, C4 (worker pipeline).
- **Done when:** One campaign with ≥2 local workers runs ≥2 shards concurrently (or overlapped), produces one merged report, and survives a single worker crash without aborting the campaign.

### C9a — Tart VM adapter **(v1)**

- **Scope:** `backend = vm` via **Tart** on Apple Silicon: create/reuse VM, install/copy app, run worker inside guest, pull artifacts, recycle/teardown; document Tart install + image prerequisites; graceful error if Tart/Apple Silicon unavailable (tell user to use `local`).
- **Specs:** 2.9 vm path.
- **Dependencies:** C8; Tart available on the host.
- **Done when:** Same campaign with `backend = vm` and `workers ≥ 2` runs shards in separate Tart VMs, merges one report, and survives one VM/worker failure without killing the campaign.

### C9b — Cloud interface + spend (paid host post-v1)

- **Scope:** Pluggable `cloud` adapter surface; **enforce `max_spend` (default $10 USD)** with spend meter and `drain`/`cancel`; stub or single thin cloud path optional. **Do not** hard-require AWS Mac / MacStadium / etc. for v1.
- **Specs:** rest of 2.9 cloud + spend.
- **Dependencies:** C8; C9a preferred so vm/cloud share bootstrap patterns.
- **Done when (v1 bar):** spend-cap plumbing works end-to-end against a metered adapter (even a local cost-simulator). **Done when (post-v1):** a real paid Mac host adapter completes shards under `max_spend`.

### C3 note — recorded flows

- v1 format is the **owned JSON action list** (not a third-party recorder export).

### C10 — iOS Simulator

- **Scope:** Simulator boot, install, drive (XCUITest and/or a11y); reuse intents/reporter/PR loop; fleet can run multiple simulators/workers where the backend allows.
- **Specs:** Port 2.1 behavior to iOS; reuse 2.2–2.9.
- **Dependencies:** macOS chunks stable; Xcode/Simulator; C8 for multi-sim.
- **Done when:** Same NL smoke intent runs against an iOS sample with artifacts + optional HITL PR; optional multi-sim campaign documented.

### C11 — Later target surfaces (explicitly out of depth here)

- Web/web apps: new **target** drivers + thin adapters to existing Orchestrator/Reporter/PR loop. Plan when macOS+iOS are solid. (VM/cloud as *execution backends* are already C9 — do not confuse with web as a product under test.)

---

## 4. Agent ownership map

Prefer **one agent per coherent chunk**, not per tiny task. Coordinator keeps cross-cutting sequencing and this plan.

| Chunk / feature | Ownership | Suggested agent label |
| --- | --- | --- |
| C1 Config / CLI / report layout | Dedicated agent | **Build CLI and config skeleton** |
| C2 macOS App Driver | Dedicated agent | **Build macOS app driver** |
| C3 Intent Ingestion | Dedicated agent (or shared with Explorer if same owner) | **Build intent ingestion** |
| C4 Scripted Explorer + Reporter | Dedicated agent | **Build scripted explorer and reporter** |
| C5 Exploratory hunting | Same agent as C4 (coherent “Explorer”) | **Extend explorer for bug hunting** |
| C6 Visual Diff | Dedicated agent | **Build visual diff and baselines** |
| C7 Fix + PR Loop | Dedicated agent | **Build fix and PR retest loop** |
| C8 Run Orchestrator (local fleet) | Dedicated agent | **Build parallel run orchestrator** |
| C9a Tart VM adapter (v1) | Dedicated agent | **Build Tart VM worker adapter** |
| C9b Cloud interface + spend | Same as C9a or follow-on | **Add cloud adapter interface and spend caps** |
| C10 iOS Simulator driver | Dedicated agent | **Build iOS Simulator driver** |
| C11 Web targets | Future dedicated agents | *(name when phased in)* |
| Cross-cutting sample apps, fixtures, docs polish | Project coordinator / shared owner | **Maintain fixtures and integration demos** |
| Plan updates, chunk sequencing, acceptance review | Project coordinator | **Coordinate Autonomous QA delivery** |

**Recommendation:** Keep Orchestrator separate from App Driver — driver is “control one app session”; orchestrator is “schedule many sessions and merge.” Do not split Driver into screenshot vs click agents. Keep Explorer+Visual as two agents once Visual starts. Reporter merge logic stays with Orchestrator+Reporter owners closely coordinated; MVP may ship merge inside the Orchestrator agent with Reporter schema owned by C4.

---

## 5. Out of scope / later

- **Web and web apps as products under test** — future target driver; reuse Orchestrator, Reporter, and PR loop.
- **Building a custom cloud or hypervisor** — out of scope; adapt existing VM/cloud providers.
- **Unlimited free scale** — capacity is user-supplied; **cloud spend is always capped** by user-configured `max_spend` (plus time/worker budgets). The product exposes knobs, not infinite machines.
- **Hosted multi-tenant SaaS control plane** — not required for CLI/agent value; live `status` + merged files first.
- **Auto-merge and production deploy** — human or separate release tooling.
- **Figma/design-tool sync, full accessibility audit product** — adjacent; not this framework’s core.
- **Inventing a new general agent runtime** — rejected unless a chunk proves existing CLIs cannot host the loop.
- **One competing PR per worker** — explicitly forbidden; fix loop is campaign-level only.

---

## 6. Decisions locked

| Topic | Decision |
| --- | --- |
| Default PR mode | `human` |
| Issue filing | GitHub + Linear issue creation enabled; user-provided **issue format/template** (optimizable) so findings log in their format |
| Video | Default **always record**; toggle for `on_failure` or `exploratory_only` |
| Default workers | `2` (small local fleet) |
| Default / sample `max_spend` | **$10 USD** (`aqa init` cloud comments + suggested default when enabling cloud) |
| Target app for early testing | **Defer** — build the framework first; pick what we point it at later (no demo gate) |
| Recorded-flow format (v1) | **Simple JSON action list we own**; created via **`aqa record`**, hand edit, or agent; importers later if needed |
| Bug artifacts on issue/PR | **Failure video + screenshots + steps** always candidates for the finding/issue/PR; plus **JSON replay stub** so the flow can be re-run |
| Cloud/VM (v1) | **Local + Tart in v1** (Tart after local fleet; not a gate on C1–C4). Paid Mac cloud host **post-v1**; keep cloud pluggable + `$10` spend metering |

### Recorded flows — pros/cons (why JSON)

| Option | Pros | Cons |
| --- | --- | --- |
| **A. Simple JSON we define (chosen)** | Fully under our control; agents can author/edit it; easy to diff in git; no third-party lock-in; fastest path to “replay these steps” | No one-click import from an existing recorder on day one |
| **B. Adopt an existing recorder export now** | Familiar if you already record in that tool; less “invent a format” | Couples MVP to someone else’s schema/versioning; we may not know which tool you use; slows the first build |

**Recommendation (locked):** A. Define a small JSON step list; add importers later only when a real recorder shows up in use.

### Cloud/VM — decision (updated)

| Option | Pros | Cons |
| --- | --- | --- |
| **Paid Mac cloud in v1** | Real remote scale | Accounts/billing/images before core loop is proven |
| **Local only in v1** | Fastest path | Parallel GUI on one display stays flaky |
| **Local + Tart in v1 (chosen)** | Real isolation for multi-worker GUI without a cloud bill; proves the `vm` backend early | Needs Apple Silicon + Tart images; more moving parts than local-only |

**Recommendation (locked):** Include **Tart in v1**, sequenced **after** local single-worker + local fleet (C1–C8). Do **not** block the first green local path on Tart. Paid cloud host stays post-v1.

### Decision backends + UX friction (locked)

| Choice | Lock |
| --- | --- |
| Language | **Python** in `swarmqa/` — no Rust/PyO3 for decision or friction |
| Default decide mode | `heuristic` (CI / FakeDriver unchanged) |
| Opt-in cascade | `heuristic → system_one → computer_use` on stall; fail-open |
| System One | Typed chooser over finite a11y candidates (HTTP or fake); labels leave the machine when HTTP is on — see `docs/decision.md` |
| Computer use | Shell-out JSON command (or fake); act via `AppDriver` |
| UX friction | Gold-relative session metrics → advisory `friction_path`; `fail_ci = false` default; see `docs/friction.md` |

## 7. Still open

None blocking build. Target app for live runs will be chosen when integration testing starts.
)