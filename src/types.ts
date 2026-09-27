/** Locked / supported config enums and defaults for C1. */

export const PR_MODES = ["human"] as const;
export type PrMode = (typeof PR_MODES)[number];

export const VIDEO_MODES = ["always"] as const;
export type VideoMode = (typeof VIDEO_MODES)[number];

export const BACKENDS = ["local", "vm", "cloud"] as const;
export type Backend = (typeof BACKENDS)[number];

export const DEFAULTS = {
  prMode: "human" as PrMode,
  workers: 2,
  videoMode: "always" as VideoMode,
  backend: "local" as Backend,
  /** Sample cloud spend cap (USD). Required when backend is cloud. */
  maxSpendUsd: 10,
} as const;

export interface AqaConfig {
  pr: { mode: PrMode };
  workers: number;
  video: { mode: VideoMode };
  backend: Backend;
  /** Present when backend is cloud (and optionally otherwise). */
  max_spend?: number;
}

export interface ConfigOverrides {
  prMode?: string;
  workers?: number;
  videoMode?: string;
  backend?: string;
  maxSpend?: number;
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}
