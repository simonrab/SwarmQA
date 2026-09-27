# SwarmQA

Autonomous QA toolkit. C1 ships the `aqa` TypeScript CLI skeleton: config, report layout, and command stubs.

## Quick start

```bash
npm install
npm run build
npm link   # optional — puts `aqa` on PATH

aqa init
aqa run                 # no-op → aqa-reports/campaign-*/ with empty layout
aqa report
aqa report --json
aqa baseline update     # stub
aqa status              # stub
aqa record              # stub
```

Exit codes: `0` success/no-op, `1` general failure, `2` invalid config.

```bash
npm test
```

## Config (`aqa.config.toml`)

Locked C1 defaults:

| Field | Default |
|-------|---------|
| `pr.mode` | `human` |
| `workers` | `2` |
| `video.mode` | `always` |
| `backend` | `local` (`local` \| `vm` \| `cloud`) |
| `max_spend` | required when `backend = "cloud"` (sample: `10` USD) |

CLI overrides: `--pr-mode`, `--workers`, `--video-mode`, `--backend`, `--max-spend`.

## Report layout

```
aqa-reports/campaign-<id>/
  summary.md
  summary.json
  workers/
  findings/
  media/
  raw/
```
