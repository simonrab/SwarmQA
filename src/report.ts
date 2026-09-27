import fs from "node:fs";
import path from "node:path";
import type { AqaConfig } from "./types.js";

export const REPORTS_DIR = "aqa-reports";

export interface CampaignPaths {
  root: string;
  summaryMd: string;
  summaryJson: string;
  workers: string;
  findings: string;
  media: string;
  raw: string;
}

export function campaignId(now: Date = new Date()): string {
  const iso = now.toISOString().replace(/[:.]/g, "-");
  return `campaign-${iso}`;
}

export function campaignPaths(root: string): CampaignPaths {
  return {
    root,
    summaryMd: path.join(root, "summary.md"),
    summaryJson: path.join(root, "summary.json"),
    workers: path.join(root, "workers"),
    findings: path.join(root, "findings"),
    media: path.join(root, "media"),
    raw: path.join(root, "raw"),
  };
}

/** Create empty C1 report layout under aqa-reports/campaign-*. */
export function createCampaignLayout(
  cwd: string,
  config: AqaConfig,
  id: string = campaignId(),
): CampaignPaths {
  const root = path.join(cwd, REPORTS_DIR, id);
  const paths = campaignPaths(root);

  fs.mkdirSync(paths.workers, { recursive: true });
  fs.mkdirSync(paths.findings, { recursive: true });
  fs.mkdirSync(paths.media, { recursive: true });
  fs.mkdirSync(paths.raw, { recursive: true });

  const summary = {
    campaign_id: id,
    status: "noop",
    created_at: new Date().toISOString(),
    config: {
      pr_mode: config.pr.mode,
      workers: config.workers,
      video_mode: config.video.mode,
      backend: config.backend,
      max_spend: config.max_spend ?? null,
    },
    findings_count: 0,
    workers_count: 0,
  };

  const md = [
    `# Campaign ${id}`,
    "",
    `Status: **${summary.status}** (C1 run is a no-op)`,
    "",
    "## Config",
    "",
    `- pr.mode: \`${config.pr.mode}\``,
    `- workers: \`${config.workers}\``,
    `- video.mode: \`${config.video.mode}\``,
    `- backend: \`${config.backend}\``,
    `- max_spend: \`${config.max_spend ?? "n/a"}\``,
    "",
    "## Findings",
    "",
    "_None yet._",
    "",
  ].join("\n");

  fs.writeFileSync(paths.summaryMd, md, "utf8");
  fs.writeFileSync(paths.summaryJson, JSON.stringify(summary, null, 2) + "\n", "utf8");

  return paths;
}

export function listCampaigns(cwd: string): string[] {
  const base = path.join(cwd, REPORTS_DIR);
  if (!fs.existsSync(base)) {
    return [];
  }
  return fs
    .readdirSync(base, { withFileTypes: true })
    .filter((d) => d.isDirectory() && d.name.startsWith("campaign-"))
    .map((d) => d.name)
    .sort();
}

export function latestCampaign(cwd: string): CampaignPaths | null {
  const names = listCampaigns(cwd);
  if (names.length === 0) {
    return null;
  }
  const latest = names[names.length - 1]!;
  return campaignPaths(path.join(cwd, REPORTS_DIR, latest));
}
