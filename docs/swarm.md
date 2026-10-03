# Swarm scheduler

`swarmqa.orchestrator.swarm` lets one campaign run many devices at once.
Each interactive shard leases its own device from a `DevicePool`. Crawl
workers split the untried controls between them instead of all tapping the
same buttons. Every worker's screen graph is merged into one campaign graph,
and later workers start from that graph.

Without a `[swarm]` table, or with `pool = "none"`, the campaign runs
exactly as before: the same worker slots, progress lines, files and summary.

## The `[swarm]` table

```toml
[swarm]
pool = "ios"              # "none" (default), "ios", "tart", "local_mac" or "auto"
devices = 6               # devices to run at once (default: the pool's capacity)
golden = "iPhone 17"      # iOS: a shut-down simulator to clone (name or UDID)
device_type = "iPhone 17" # iOS: `simctl create` device type when there is no golden
runtime = "iOS-26-5"      # iOS: runtime for `simctl create` (default: the newest)
erase_mode = "uninstall"  # iOS: "uninstall" (fast) or "erase" (a cold boot on every lease)
image = "macos-base"      # tart: the VM image to clone
acquire_timeout_s = 600   # how long a shard waits for a free device
erase_on_release = true   # wipe app state when a lease ends
share_frontier = true     # crawl workers split the frontier (default: on when a pool is set)
merge_graphs = true       # merge screen graphs and seed later workers (default: on when a pool is set)
```

Unknown keys and bad values raise `SwarmConfigError`, a `ConfigError`.

| `pool` | Pool | Platform |
|---|---|---|
| `none` | no pool; drivers choose their own device as before | any |
| `ios` | `IOSSimulatorPool`: clones a golden simulator, one clone per slot | `ios` |
| `local_mac` | `LocalMacPool`: the local Mac, one lease at a time | `macos` |
| `tart` | `TartVMPool`: Tart macOS VMs, at most two per host | `macos` |
| `auto` | `ios` for an iOS app; `tart` for macOS when `image` is set, else `local_mac` | |

For `ios` with neither `golden` nor `device_type`, the pool clones
`app.simulator` when it is a UDID. Otherwise it creates simulators of the
type `app.simulator` names (default `iPhone 17`). For `ios` and `tart`,
`devices` caps the host capacity from `swarmqa.devices.capacity`; it never
raises it. See docs/devices.md for what a lease does on the device.

A pool is used only by the `local` backend. The `vm` and `cloud` backends
provide their own machines, so a pool set for them is ignored with a
warning.

## Scheduling

- **Worker slots.** With a pool, the slots are
  `min(workers, devices or capacity)`. `status.json` reports this as
  `workers_configured`. If the pool has no device for `app.platform`, each
  interactive shard fails with an error at once instead of waiting.
- **Leases.** Scripted, exploratory and visual shards each lease one device
  (`with lease(pool, app.platform, build)`), run on it, and release it when
  they finish, even if the worker raised. The `build` is a `BuildArtifact`
  made from `app.path`, `app.bundle_id` and `app.platform`, or `None` when
  `app.path` is unset. Suite shards need no device and lease nothing. Before
  each lease, a progress line with the step `lease` is printed:
  `[worker w2] shard=checkout mode=exploratory step=lease`.
- **Lease failures.** A shard that cannot get a device ends with status
  `error` and the shard's executor never runs:
  - `DeviceUnavailable`: `no ios device available: ...`
  - `DeviceSetupError` (clone, boot or install failed):
    `device setup failed: ...`, which reports a broken build or device
    rather than a capacity shortage.
- **Device to driver.** The leased device reaches the driver through
  `swarmqa.backends.local.device_driver_factory`.
  - The default factory pins iOS drivers (`legacy` and `runner`) to the
    lease's UDID through their `udid` argument. Workers are threads, so the
    process-wide `SWARMQA_SIMULATOR_UDID` is not used for this.
  - A custom `driver_factory(target, work_dir, *, device)` receives the
    `Device`. A factory without a `device` parameter is called as before.
  - With `local.isolation = "subprocess"`, the worker process gets
    `SWARMQA_SIMULATOR_UDID` in its environment.
  - A Tart VM lease cannot be driven by the local backend's drivers, so it
    fails with an error naming the `vm` backend.
- **Close.** The campaign owns the pool and closes it at the end, which
  releases every lease and deletes the clones the pool created. This also
  applies to a pool passed in as `run_campaign(..., device_pool=pool)`.

## Shared frontier

When `share_frontier` is on, crawl shards (exploratory shards with no
`goal`) get a
`claim(screen_id, control_key)` hook from the campaign's `FrontierBoard`.
This works only with the agent engine and in-process workers.

- The first worker to claim an untried control owns it. Any other worker
  that asks is refused, marks the control tried in its own graph, and moves
  on. Two or three crawl workers on the same app therefore split its
  controls, and together they still try every one.
- When a worker finishes, for any reason, the claims it holds but never
  tried are freed so a later worker can take them. A worker that left no
  screen graph frees all of its claims.
- Goal shards never claim. They get only the seed graph.

## Campaign graph

When `merge_graphs` is on, each finished agent-loop worker's
`workers/<id>/screen_graph.json` is merged into
`<campaign>/screen_graph.json`, using `ScreenGraph.merge`: screens,
controls and tried sets are unioned and visit and edge counts are summed.
Controls a worker marked tried only because another worker held the claim
stay untried in the campaign graph.

- **Seeding.** Every later exploratory shard, crawl or goal, starts with a
  snapshot of the campaign graph as `seed_graph`. A crawl worker that starts
  after the app is fully mapped clicks nothing.
- **Resume.** A resumed campaign starts from the saved graph.
- **Summary.** `summary.md` gets a `## Devices` section (pool, leases,
  distinct devices) when a pool is used, and a `## Screen graph` section
  (screens, controls tried of controls, frontier screens) when a graph was
  merged. `summary.json` does not change, because `CampaignResult` has no
  field for either section.

## Speed

Workers spend most of their time waiting on the device, so N devices finish
about N times faster. In `tests/test_swarm.py::test_n_devices_finish_about_n_times_faster`,
six shards with a 0.2 s step each took 1.33 s on one fake device, 0.52 s on
three (2.6x) and 0.27 s on six (4.9x). The Phase 6 target is 20 flows on 6
iOS simulators in under 10 minutes on cached runs. That fits if each cached
flow replays in about 2.5 minutes, plus warm boots: with
`erase_mode = "uninstall"`, a clone stays booted between leases.
