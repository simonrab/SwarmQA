#!/usr/bin/env node
import fs from "node:fs";
import { Command } from "commander";
import {
  applyOverrides,
  loadConfigFile,
  writeInitConfig,
} from "./config.js";
import { EXIT } from "./exit.js";
import {
  createCampaignLayout,
  latestCampaign,
} from "./report.js";
import { ConfigError, type ConfigOverrides } from "./types.js";

function die(code: number, message: string): never {
  console.error(message);
  process.exit(code);
}

function collectOverrides(opts: {
  prMode?: string;
  workers?: string;
  videoMode?: string;
  backend?: string;
  maxSpend?: string;
}): ConfigOverrides {
  const overrides: ConfigOverrides = {};
  if (opts.prMode !== undefined) overrides.prMode = opts.prMode;
  if (opts.workers !== undefined) {
    const n = Number(opts.workers);
    if (!Number.isFinite(n)) {
      throw new ConfigError("--workers must be a number");
    }
    overrides.workers = n;
  }
  if (opts.videoMode !== undefined) overrides.videoMode = opts.videoMode;
  if (opts.backend !== undefined) overrides.backend = opts.backend;
  if (opts.maxSpend !== undefined) {
    const n = Number(opts.maxSpend);
    if (!Number.isFinite(n)) {
      throw new ConfigError("--max-spend must be a number");
    }
    overrides.maxSpend = n;
  }
  return overrides;
}

function loadResolvedConfig(opts: {
  prMode?: string;
  workers?: string;
  videoMode?: string;
  backend?: string;
  maxSpend?: string;
  cwd?: string;
}) {
  const cwd = opts.cwd ?? process.cwd();
  const base = loadConfigFile(cwd);
  return applyOverrides(base, collectOverrides(opts));
}

function addConfigFlags(cmd: Command): Command {
  return cmd
    .option("--pr-mode <mode>", "Override pr.mode")
    .option("--workers <n>", "Override workers")
    .option("--video-mode <mode>", "Override video.mode")
    .option("--backend <backend>", "Override backend (local|vm|cloud)")
    .option("--max-spend <usd>", "Override max_spend (USD)");
}

export function createProgram(): Command {
  const program = new Command();
  program
    .name("aqa")
    .description("Autonomous QA — SwarmQA CLI")
    .version("0.1.0")
    .showHelpAfterError();

  program
    .command("init")
    .description("Write aqa.config.toml with locked C1 defaults")
    .action(() => {
      try {
        const path = writeInitConfig(process.cwd());
        console.log(`Wrote ${path}`);
      } catch (err) {
        if (err instanceof ConfigError) {
          die(EXIT.INVALID_CONFIG, err.message);
        }
        die(EXIT.FAILURE, err instanceof Error ? err.message : String(err));
      }
    });

  addConfigFlags(
    program
      .command("run")
      .description("Run a campaign (C1: no-op; creates empty report layout)"),
  ).action((opts) => {
    try {
      const config = loadResolvedConfig(opts);
      const paths = createCampaignLayout(process.cwd(), config);
      console.log(`Created report layout: ${paths.root}`);
      console.log("(C1) run is a no-op — workers not started");
    } catch (err) {
      if (err instanceof ConfigError) {
        die(EXIT.INVALID_CONFIG, err.message);
      }
      die(EXIT.FAILURE, err instanceof Error ? err.message : String(err));
    }
  });

  addConfigFlags(
    program
      .command("report")
      .description("Print the latest campaign summary")
      .option("--json", "Emit summary.json instead of summary.md"),
  ).action((opts) => {
    try {
      // Validate config / overrides even for report (field-level checks).
      if (fs.existsSync("aqa.config.toml")) {
        loadResolvedConfig(opts);
      }
      const campaign = latestCampaign(process.cwd());
      if (!campaign) {
        die(EXIT.FAILURE, "No campaigns found under aqa-reports/. Run `aqa run` first.");
      }
      const file = opts.json ? campaign.summaryJson : campaign.summaryMd;
      if (!fs.existsSync(file)) {
        die(EXIT.FAILURE, `Missing report file: ${file}`);
      }
      process.stdout.write(fs.readFileSync(file, "utf8"));
    } catch (err) {
      if (err instanceof ConfigError) {
        die(EXIT.INVALID_CONFIG, err.message);
      }
      die(EXIT.FAILURE, err instanceof Error ? err.message : String(err));
    }
  });

  program
    .command("baseline")
    .description("Baseline operations (stub)")
    .command("update")
    .description("Update baseline (stub — owned by a later chunk)")
    .action(() => {
      console.log("baseline update: stub (not implemented in C1)");
    });

  program
    .command("status")
    .description("Show campaign/worker status (stub)")
    .action(() => {
      console.log("status: stub (not implemented in C1)");
    });

  program
    .command("record")
    .description("Record a session (stub)")
    .action(() => {
      console.log("record: stub (not implemented in C1)");
    });

  return program;
}

export async function main(argv: string[] = process.argv): Promise<void> {
  const program = createProgram();
  await program.parseAsync(argv);
}

// Avoid executing when imported from tests.
const entry = process.argv[1] ?? "";
if (entry.endsWith("cli.js") || entry.endsWith("cli.ts")) {
  main().catch((err) => {
    console.error(err instanceof Error ? err.message : String(err));
    process.exit(EXIT.FAILURE);
  });
}
