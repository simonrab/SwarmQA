import fs from "node:fs";
import path from "node:path";
import TOML from "@iarna/toml";
import {
  BACKENDS,
  ConfigError,
  DEFAULTS,
  PR_MODES,
  VIDEO_MODES,
  type AqaConfig,
  type Backend,
  type ConfigOverrides,
  type PrMode,
  type VideoMode,
} from "./types.js";

export const CONFIG_FILENAME = "aqa.config.toml";

export function defaultConfig(): AqaConfig {
  return {
    pr: { mode: DEFAULTS.prMode },
    workers: DEFAULTS.workers,
    video: { mode: DEFAULTS.videoMode },
    backend: DEFAULTS.backend,
  };
}

/**
 * Serialize config to TOML matching the C1 schema.
 * Root keys (workers, backend, max_spend) must appear before tables.
 */
export function configToToml(config: AqaConfig): string {
  const lines: string[] = [
    "# Autonomous QA configuration (C1)",
    "",
    `workers = ${config.workers}`,
    `backend = ${tomlString(config.backend)}`,
  ];
  if (config.max_spend !== undefined) {
    lines.push(`max_spend = ${config.max_spend}`);
  } else {
    lines.push(`# max_spend = ${DEFAULTS.maxSpendUsd}  # required when backend = "cloud"`);
  }
  lines.push(
    "",
    "[pr]",
    `mode = ${tomlString(config.pr.mode)}`,
    "",
    "[video]",
    `mode = ${tomlString(config.video.mode)}`,
    "",
  );
  return lines.join("\n");
}

function tomlString(value: string): string {
  return JSON.stringify(value);
}

export function findConfigPath(cwd: string = process.cwd()): string {
  return path.join(cwd, CONFIG_FILENAME);
}

export function loadConfigFile(cwd: string = process.cwd()): AqaConfig {
  const filePath = findConfigPath(cwd);
  if (!fs.existsSync(filePath)) {
    throw new ConfigError(
      `Missing ${CONFIG_FILENAME}. Run \`aqa init\` first.`,
    );
  }
  let parsed: unknown;
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    parsed = TOML.parse(raw);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new ConfigError(`Failed to parse ${CONFIG_FILENAME}: ${msg}`);
  }
  return validateAndNormalize(parsed);
}

/**
 * Field-level validation + normalization from a raw TOML object.
 */
export function validateAndNormalize(raw: unknown): AqaConfig {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new ConfigError("Config root must be a table");
  }
  const obj = raw as Record<string, unknown>;

  const pr = requireTable(obj, "pr");
  const mode = requireString(pr, "mode", "pr.mode");
  assertEnum(mode, PR_MODES, "pr.mode");

  if (!("workers" in obj)) {
    throw new ConfigError("Missing required field: workers");
  }
  const workers = obj.workers;
  if (typeof workers !== "number" || !Number.isInteger(workers) || workers < 1) {
    throw new ConfigError("workers must be a positive integer");
  }

  const video = requireTable(obj, "video");
  const videoMode = requireString(video, "mode", "video.mode");
  assertEnum(videoMode, VIDEO_MODES, "video.mode");

  if (!("backend" in obj)) {
    throw new ConfigError("Missing required field: backend");
  }
  const backend = obj.backend;
  if (typeof backend !== "string") {
    throw new ConfigError("backend must be a string");
  }
  assertEnum(backend, BACKENDS, "backend");

  let max_spend: number | undefined;
  if ("max_spend" in obj && obj.max_spend !== undefined) {
    if (typeof obj.max_spend !== "number" || !(obj.max_spend > 0)) {
      throw new ConfigError("max_spend must be a positive number (USD)");
    }
    max_spend = obj.max_spend;
  }

  if (backend === "cloud" && max_spend === undefined) {
    throw new ConfigError(
      'max_spend is required when backend = "cloud" (sample default: 10 USD)',
    );
  }

  const config: AqaConfig = {
    pr: { mode: mode as PrMode },
    workers,
    video: { mode: videoMode as VideoMode },
    backend: backend as Backend,
  };
  if (max_spend !== undefined) {
    config.max_spend = max_spend;
  }
  return config;
}

export function applyOverrides(
  config: AqaConfig,
  overrides: ConfigOverrides,
): AqaConfig {
  const next: AqaConfig = {
    pr: { mode: config.pr.mode },
    workers: config.workers,
    video: { mode: config.video.mode },
    backend: config.backend,
  };
  if (config.max_spend !== undefined) {
    next.max_spend = config.max_spend;
  }

  if (overrides.prMode !== undefined) {
    assertEnum(overrides.prMode, PR_MODES, "--pr-mode");
    next.pr.mode = overrides.prMode as PrMode;
  }
  if (overrides.workers !== undefined) {
    if (!Number.isInteger(overrides.workers) || overrides.workers < 1) {
      throw new ConfigError("--workers must be a positive integer");
    }
    next.workers = overrides.workers;
  }
  if (overrides.videoMode !== undefined) {
    assertEnum(overrides.videoMode, VIDEO_MODES, "--video-mode");
    next.video.mode = overrides.videoMode as VideoMode;
  }
  if (overrides.backend !== undefined) {
    assertEnum(overrides.backend, BACKENDS, "--backend");
    next.backend = overrides.backend as Backend;
  }
  if (overrides.maxSpend !== undefined) {
    if (!(overrides.maxSpend > 0)) {
      throw new ConfigError("--max-spend must be a positive number (USD)");
    }
    next.max_spend = overrides.maxSpend;
  }

  if (next.backend === "cloud" && next.max_spend === undefined) {
    throw new ConfigError(
      'max_spend is required when backend = "cloud" (sample default: 10 USD)',
    );
  }

  return next;
}

export function writeInitConfig(cwd: string = process.cwd()): string {
  const filePath = findConfigPath(cwd);
  if (fs.existsSync(filePath)) {
    throw new ConfigError(`${CONFIG_FILENAME} already exists`);
  }
  const config = defaultConfig();
  fs.writeFileSync(filePath, configToToml(config), "utf8");
  return filePath;
}

function requireTable(
  parent: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  const value = parent[key];
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ConfigError(`Missing or invalid table: [${key}]`);
  }
  return value as Record<string, unknown>;
}

function requireString(
  parent: Record<string, unknown>,
  key: string,
  label: string,
): string {
  const value = parent[key];
  if (typeof value !== "string") {
    throw new ConfigError(`Missing or invalid field: ${label}`);
  }
  return value;
}

function assertEnum<T extends string>(
  value: string,
  allowed: readonly T[],
  label: string,
): asserts value is T {
  if (!(allowed as readonly string[]).includes(value)) {
    throw new ConfigError(
      `Invalid ${label}: ${JSON.stringify(value)} (allowed: ${allowed.join(", ")})`,
    );
  }
}
