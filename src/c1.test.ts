import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  applyOverrides,
  configToToml,
  defaultConfig,
  validateAndNormalize,
  writeInitConfig,
} from "./config.js";
import { createCampaignLayout, latestCampaign } from "./report.js";
import { ConfigError } from "./types.js";
import { EXIT } from "./exit.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(__dirname, "cli.js");

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "aqa-c1-"));
}

function runCli(cwd: string, args: string[]): {
  status: number | null;
  stdout: string;
  stderr: string;
} {
  const result = spawnSync(process.execPath, [CLI, ...args], {
    cwd,
    encoding: "utf8",
    env: { ...process.env },
  });
  return {
    status: result.status,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
  };
}

test("1. defaultConfig locks C1 defaults", () => {
  const c = defaultConfig();
  assert.equal(c.pr.mode, "human");
  assert.equal(c.workers, 2);
  assert.equal(c.video.mode, "always");
  assert.equal(c.backend, "local");
  assert.equal(c.max_spend, undefined);
});

test("2. aqa init writes aqa.config.toml", () => {
  const dir = tmpDir();
  const file = writeInitConfig(dir);
  assert.ok(fs.existsSync(file));
  const text = fs.readFileSync(file, "utf8");
  assert.match(text, /mode = "human"/);
  assert.match(text, /workers = 2/);
  assert.match(text, /mode = "always"/);
  assert.match(text, /backend = "local"/);
});

test("3. init refuses to overwrite existing config (exit 2 via CLI)", () => {
  const dir = tmpDir();
  writeInitConfig(dir);
  const again = runCli(dir, ["init"]);
  assert.equal(again.status, EXIT.INVALID_CONFIG);
  assert.match(again.stderr, /already exists/);
});

test("4. cloud backend requires max_spend", () => {
  assert.throws(
    () =>
      validateAndNormalize({
        pr: { mode: "human" },
        workers: 2,
        video: { mode: "always" },
        backend: "cloud",
      }),
    (err: unknown) =>
      err instanceof ConfigError && /max_spend is required/.test(err.message),
  );
});

test("5. cloud backend accepts max_spend = 10", () => {
  const c = validateAndNormalize({
    pr: { mode: "human" },
    workers: 2,
    video: { mode: "always" },
    backend: "cloud",
    max_spend: 10,
  });
  assert.equal(c.backend, "cloud");
  assert.equal(c.max_spend, 10);
});

test("6. invalid backend / workers fail field-level validation", () => {
  assert.throws(
    () =>
      validateAndNormalize({
        pr: { mode: "human" },
        workers: 2,
        video: { mode: "always" },
        backend: "k8s",
      }),
    ConfigError,
  );
  assert.throws(
    () =>
      validateAndNormalize({
        pr: { mode: "human" },
        workers: 0,
        video: { mode: "always" },
        backend: "local",
      }),
    ConfigError,
  );
});

test("7. CLI flag overrides apply on top of file config", () => {
  const base = defaultConfig();
  const next = applyOverrides(base, {
    workers: 4,
    backend: "vm",
  });
  assert.equal(next.workers, 4);
  assert.equal(next.backend, "vm");
  assert.equal(next.pr.mode, "human");
});

test("8. aqa run creates empty report layout", () => {
  const dir = tmpDir();
  writeInitConfig(dir);
  const result = runCli(dir, ["run"]);
  assert.equal(result.status, EXIT.OK, result.stderr);
  const campaign = latestCampaign(dir);
  assert.ok(campaign);
  for (const sub of ["workers", "findings", "media", "raw"] as const) {
    assert.ok(fs.existsSync(campaign[sub]), sub);
  }
  assert.ok(fs.existsSync(campaign.summaryMd));
  assert.ok(fs.existsSync(campaign.summaryJson));
});

test("9. aqa report and aqa report --json read latest campaign", () => {
  const dir = tmpDir();
  writeInitConfig(dir);
  createCampaignLayout(dir, defaultConfig(), "campaign-test");
  const md = runCli(dir, ["report"]);
  assert.equal(md.status, EXIT.OK, md.stderr);
  assert.match(md.stdout, /Campaign campaign-test/);
  const json = runCli(dir, ["report", "--json"]);
  assert.equal(json.status, EXIT.OK, json.stderr);
  const parsed = JSON.parse(json.stdout);
  assert.equal(parsed.campaign_id, "campaign-test");
  assert.equal(parsed.status, "noop");
});

test("10. stubs exit 0; invalid override exits 2", () => {
  const dir = tmpDir();
  writeInitConfig(dir);
  for (const args of [["status"], ["record"], ["baseline", "update"]] as string[][]) {
    const r = runCli(dir, args);
    assert.equal(r.status, EXIT.OK, `${args.join(" ")}: ${r.stderr}`);
  }
  const bad = runCli(dir, ["run", "--backend", "cloud"]);
  assert.equal(bad.status, EXIT.INVALID_CONFIG);
  assert.match(bad.stderr, /max_spend/);

  // configToToml round-trip sanity
  const toml = configToToml(defaultConfig());
  assert.match(toml, /\[pr\]/);
});
