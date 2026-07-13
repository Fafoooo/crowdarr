import { useCallback, useEffect, useState } from "react";

import { apiRequest, ApiError } from "../api/client";
import { CheckIcon, RefreshIcon, RepairIcon } from "../components/icons";
import {
  Button,
  ErrorNotice,
  LoadingBlock,
  PageHeader,
  Panel,
} from "../components/ui";
import type {
  ActionResponse,
  ActivityItem,
  ConnectorHealth,
  DashboardData,
  JobResponse,
  StuckTorrent,
} from "../types";

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Unexpected error";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const repairOutcomes: readonly StuckTorrent["repair_outcome"][] = [
  "dry_run",
  "fetch_failed",
  "fixed",
  "mismatch",
  "not_applicable",
  "not_available",
  "placed",
  "ready",
  "verification_pending",
  "verified_incomplete",
];

const torrentReasons: readonly StuckTorrent["reason"][] = [
  "inspection_failed",
  "invalid_nfo_path",
  "no_incomplete_nfo",
  "no_video",
  "ready",
  "video_incomplete",
];

function repairOutcome(value: unknown, repairable: boolean) {
  return typeof value === "string" &&
    repairOutcomes.includes(value as StuckTorrent["repair_outcome"])
    ? (value as StuckTorrent["repair_outcome"])
    : repairable
      ? "ready"
      : "not_applicable";
}

function torrentReason(value: unknown, repairable: boolean) {
  return typeof value === "string" &&
    torrentReasons.includes(value as StuckTorrent["reason"])
    ? (value as StuckTorrent["reason"])
    : repairable
      ? "ready"
      : "inspection_failed";
}

function normaliseDashboard(raw: unknown): DashboardData {
  const value = isRecord(raw) ? raw : {};
  const counters = isRecord(value.counters) ? value.counters : {};
  const connectorHealth = isRecord(value.connector_health)
    ? value.connector_health
    : {};
  const connectors = Array.isArray(value.connectors)
    ? (value.connectors as ConnectorHealth[])
    : Object.entries(connectorHealth).map(([id, health]) => {
        const detail = isRecord(health) ? health : {};
        const rawStatus =
          typeof detail.status === "string" ? detail.status : "unknown";
        const status = [
          "healthy",
          "unhealthy",
          "degraded",
          "disabled",
          "unknown",
        ].includes(rawStatus)
          ? (rawStatus as ConnectorHealth["status"])
          : "unknown";
        return {
          id,
          latency_ms:
            typeof detail.latency_ms === "number" ? detail.latency_ms : null,
          message:
            typeof detail.message === "string"
              ? detail.message
              : typeof detail.detail === "string"
                ? detail.detail
                : status,
          name: typeof detail.name === "string" ? detail.name : id,
          status,
        };
      });
  const stuckTorrents = Array.isArray(value.stuck_torrents)
    ? value.stuck_torrents.flatMap((item) => {
        if (!isRecord(item)) return [];
        const missingNfoPath =
          typeof item.missing_nfo_path === "string"
            ? item.missing_nfo_path
            : "";
        const repairable =
          typeof item.repairable === "boolean"
            ? item.repairable
            : missingNfoPath.length > 0;
        return [
          {
            category:
              typeof item.category === "string"
                ? item.category
                : "uncategorized",
            hash: typeof item.hash === "string" ? item.hash : "",
            missing_nfo_count:
              typeof item.missing_nfo_count === "number"
                ? item.missing_nfo_count
                : missingNfoPath
                  ? 1
                  : 0,
            missing_nfo_path: missingNfoPath,
            name: typeof item.name === "string" ? item.name : "Unknown torrent",
            progress: typeof item.progress === "number" ? item.progress : 0,
            reason: torrentReason(item.reason, repairable),
            repairable,
            repair_message:
              typeof item.repair_message === "string"
                ? item.repair_message
                : undefined,
            repair_outcome: repairOutcome(item.repair_outcome, repairable),
            retryable:
              typeof item.retryable === "boolean" ? item.retryable : repairable,
            state: typeof item.state === "string" ? item.state : "unknown",
          },
        ];
      })
    : [];

  return {
    connectors,
    counters: {
      fetched: typeof counters.fetched === "number" ? counters.fetched : 0,
      matches: typeof counters.matches === "number" ? counters.matches : 0,
      misses: typeof counters.misses === "number" ? counters.misses : 0,
      placed: typeof counters.placed === "number" ? counters.placed : 0,
      repaired: typeof counters.repaired === "number" ? counters.repaired : 0,
      uploaded: typeof counters.uploaded === "number" ? counters.uploaded : 0,
    },
    dry_run: typeof value.dry_run === "boolean" ? value.dry_run : true,
    recent_activity: Array.isArray(value.recent_activity)
      ? (value.recent_activity as DashboardData["recent_activity"])
      : [],
    stuck_torrents: stuckTorrents,
  };
}

const statusStyles: Record<string, string> = {
  degraded: "bg-amber-400",
  disabled: "bg-zinc-600",
  healthy: "bg-emerald-400",
  unhealthy: "bg-red-400",
  unknown: "bg-zinc-500",
};

function ConnectorCard({ connector }: { connector: ConnectorHealth }) {
  const label =
    connector.status === "healthy"
      ? "Healthy"
      : connector.status === "degraded"
        ? "Limited"
        : connector.status === "unhealthy"
          ? "Unavailable"
          : connector.status === "disabled"
            ? "Disabled"
            : "Unknown";
  return (
    <article className="rounded-xl border border-white/[0.06] bg-zinc-950/45 p-4 transition hover:border-white/10 hover:bg-zinc-950/70">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-zinc-100">
            {connector.name}
          </h3>
          <p
            className="mt-1 line-clamp-3 min-h-12 text-xs leading-4 text-zinc-500"
            title={connector.message}
          >
            {connector.message}
          </p>
        </div>
        <span className="flex shrink-0 items-center gap-1.5 rounded-full border border-white/[0.07] bg-black/20 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-300">
          <span
            aria-label={connector.status}
            className={`size-2 rounded-full ${statusStyles[connector.status] ?? statusStyles.unknown} ${connector.status === "healthy" ? "shadow-[0_0_9px_rgb(52_211_153/0.7)]" : ""}`}
            role="img"
          />
          {label}
        </span>
      </div>
      <p className="mt-4 text-[11px] font-medium uppercase tracking-wider text-zinc-600">
        {connector.latency_ms == null
          ? connector.status
          : `${connector.latency_ms} ms latency`}
      </p>
    </article>
  );
}

const repairFunnel = [
  ["CrowdNFO matches", "matches", "Lookup returned an exact NFO"],
  ["NFOs fetched", "fetched", "Raw bytes downloaded"],
  ["NFOs placed", "placed", "Atomically written into the release"],
  ["Verified repairs", "repaired", "Torrent reached 100% and is seeding"],
] as const;

const outcomeLabels: Record<
  DashboardData["stuck_torrents"][number]["repair_outcome"],
  { label: string; style: string }
> = {
  dry_run: {
    label: "Simulation ready",
    style: "border-sky-400/20 bg-sky-400/10 text-sky-200",
  },
  fetch_failed: {
    label: "Fetch failed – retry",
    style: "border-red-400/20 bg-red-400/10 text-red-200",
  },
  fixed: {
    label: "Fixed",
    style: "border-emerald-400/20 bg-emerald-400/10 text-emerald-200",
  },
  mismatch: {
    label: "NFO mismatch",
    style: "border-red-400/20 bg-red-400/10 text-red-200",
  },
  not_applicable: {
    label: "Not an NFO issue",
    style: "border-zinc-500/20 bg-zinc-500/10 text-zinc-400",
  },
  not_available: {
    label: "Not in CrowdNFO",
    style: "border-amber-400/20 bg-amber-400/10 text-amber-200",
  },
  placed: {
    label: "Placed · recheck off",
    style: "border-sky-400/20 bg-sky-400/10 text-sky-200",
  },
  ready: {
    label: "Ready · lookup on repair",
    style: "border-sky-400/20 bg-sky-400/10 text-sky-200",
  },
  verification_pending: {
    label: "Recheck still pending",
    style: "border-amber-400/20 bg-amber-400/10 text-amber-200",
  },
  verified_incomplete: {
    label: "NFO verified · other data missing",
    style: "border-amber-400/20 bg-amber-400/10 text-amber-200",
  },
};

const reasonLabels: Record<
  DashboardData["stuck_torrents"][number]["reason"],
  string
> = {
  inspection_failed: "File list could not be inspected",
  invalid_nfo_path: "NFO path is unsafe or invalid",
  no_incomplete_nfo: "No incomplete NFO detected",
  no_video: "No video payload detected",
  ready: "NFO-only repair ready",
  video_incomplete: "Video data is below 99%",
};

const JOB_POLL_INTERVAL_MS = 1_000;
const JOB_POLL_ATTEMPTS = 3_700;

function wait(delayMs: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, delayMs));
}

async function waitForJob(jobId: string): Promise<JobResponse | undefined> {
  for (let attempt = 0; attempt < JOB_POLL_ATTEMPTS; attempt += 1) {
    const job = await apiRequest<JobResponse>(
      `/api/jobs/${encodeURIComponent(jobId)}`,
    );
    if (job.status !== "queued" && job.status !== "running") return job;
    await wait(JOB_POLL_INTERVAL_MS);
  }
  return undefined;
}

function activityAccent(item: ActivityItem): string {
  if (item.status === "success") return "bg-emerald-400";
  if (item.status === "warning") return "bg-amber-400";
  if (item.status === "error") return "bg-red-400";
  return "bg-sky-400";
}

function TorrentCard({
  dryRun,
  onRepair,
  pending,
  torrent,
}: {
  dryRun: boolean;
  onRepair: () => void;
  pending: boolean;
  torrent: StuckTorrent;
}) {
  const outcome = outcomeLabels[torrent.repair_outcome];
  const unavailable = torrent.repair_outcome === "not_available";
  const retry = torrent.repair_outcome === "fetch_failed";

  return (
    <article
      aria-label={torrent.name}
      className="grid gap-4 border-t border-white/[0.05] px-4 py-4 transition first:border-t-0 hover:bg-white/[0.02] sm:px-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(11rem,1fr)_minmax(0,1.2fr)_5rem_auto] lg:items-center"
    >
      <div className="min-w-0">
        <p
          className="truncate text-sm font-semibold text-zinc-100"
          title={torrent.name}
        >
          {torrent.name}
        </p>
        <p className="mt-1 flex flex-wrap gap-2 text-[11px] text-zinc-600">
          <span>{torrent.category || "uncategorized"}</span>
          <span aria-hidden="true">·</span>
          <span>{torrent.state}</span>
        </p>
      </div>

      <div className="min-w-0">
        <span
          className={`inline-flex rounded-full border px-2.5 py-1 text-[11px] font-semibold ${outcome.style}`}
        >
          {outcome.label}
        </span>
        <p className="mt-1.5 text-xs leading-4 text-zinc-500">
          {torrent.repair_message ??
            reasonLabels[torrent.reason] ??
            torrent.reason}
        </p>
      </div>

      <div className="min-w-0 text-xs text-zinc-400">
        {torrent.missing_nfo_count > 1 ? (
          <p className="font-medium text-zinc-300">
            {torrent.missing_nfo_count} NFO files
          </p>
        ) : null}
        {torrent.missing_nfo_path ? (
          <p className="truncate" title={torrent.missing_nfo_path}>
            {torrent.missing_nfo_path}
          </p>
        ) : (
          <span className="text-zinc-700">No incomplete NFO path</span>
        )}
      </div>

      <div>
        <p className="font-mono text-sm font-semibold text-amber-300">
          {(torrent.progress * 100).toFixed(1)}%
        </p>
        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-zinc-800">
          <div
            className="h-full rounded-full bg-amber-400"
            style={{ width: `${Math.min(100, torrent.progress * 100)}%` }}
          />
        </div>
      </div>

      <div className="lg:text-right">
        {unavailable ? (
          <Button
            aria-label={`Not available — ${torrent.name}`}
            disabled
            type="button"
          >
            Not available
          </Button>
        ) : torrent.repairable ? (
          <Button
            aria-label={`${dryRun ? "Simulate repair" : retry ? "Retry repair" : "Repair"} ${torrent.name}`}
            disabled={pending}
            onClick={onRepair}
            type="button"
          >
            <RepairIcon className="size-4" />
            {dryRun ? "Simulate" : retry ? "Retry" : "Repair"}
          </Button>
        ) : (
          <span className="text-xs text-zinc-600">No repair action</span>
        )}
      </div>
    </article>
  );
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData>();
  const [loadError, setLoadError] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [actionMessage, setActionMessage] = useState<string>();
  const [actionTone, setActionTone] = useState<"info" | "success" | "warning">(
    "info",
  );
  const [pendingAction, setPendingAction] = useState<string>();
  const repairIsRunning =
    pendingAction?.startsWith("repair-") &&
    actionMessage === "Repair is running…";
  const repairableTorrents =
    data?.stuck_torrents.filter((torrent) => torrent.repairable).length ?? 0;
  const nfoRepairTorrents =
    data?.stuck_torrents.filter((torrent) => torrent.reason === "ready") ?? [];
  const otherIncompleteTorrents =
    data?.stuck_torrents.filter((torrent) => torrent.reason !== "ready") ?? [];

  const loadDashboard = useCallback(async () => {
    setLoadError(undefined);
    try {
      setData(normaliseDashboard(await apiRequest<unknown>("/api/dashboard")));
    } catch (error) {
      setLoadError(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  const runAction = async (key: string, path: string) => {
    setPendingAction(key);
    setActionError(undefined);
    setActionMessage(undefined);
    setActionTone("info");
    try {
      const response = await apiRequest<ActionResponse>(path, {
        method: "POST",
      });
      if (data?.dry_run) {
        setActionTone("warning");
        setActionMessage(
          "Simulation queued — dry run will not write files and will not recheck qBittorrent.",
        );
      } else if (key.startsWith("repair-") && response.job_id) {
        setActionTone("info");
        setActionMessage("Repair is running…");
        const job = await waitForJob(response.job_id);
        if (job === undefined) {
          setActionTone("info");
          setActionMessage(
            "Repair is still running — check Recent activity for the result.",
          );
        } else {
          await loadDashboard();
          if (job.result.message && job.result.outcome === "not_available") {
            setActionTone("warning");
            setActionMessage(job.result.message);
          } else if (job.status === "success") {
            setActionTone("success");
            setActionMessage(
              job.result.message
                ? `Repair completed successfully — ${job.result.message}`
                : "Repair completed successfully.",
            );
          } else if (job.status === "dry_run") {
            setActionTone("warning");
            setActionMessage(
              "Repair finished as a dry-run simulation; no files were changed.",
            );
          } else {
            setActionMessage(undefined);
            setActionError(
              job.result.message ??
                job.result.detail ??
                `Repair ${job.status} — see Recent activity for the exact reason.`,
            );
          }
        }
      } else {
        setActionTone("info");
        setActionMessage(response.message ?? "Action accepted");
      }
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setPendingAction(undefined);
    }
  };

  return (
    <div className="space-y-7">
      <PageHeader
        actions={
          <Button
            disabled={pendingAction === "scan"}
            onClick={() => void runAction("scan", "/api/actions/scan-repair")}
            type="button"
            variant="primary"
          >
            <RefreshIcon
              className={pendingAction === "scan" ? "animate-spin" : ""}
            />
            {data?.dry_run ? "Simulate scan" : "Scan & repair now"}
          </Button>
        }
        description="Repair incomplete releases, monitor every connector, and keep your NFO pipeline moving."
        eyebrow="Operations"
        title="Dashboard"
      />

      {actionMessage ? (
        <div
          className={`flex items-center gap-2 rounded-xl border px-4 py-3 text-sm ${
            actionTone === "warning"
              ? "border-amber-400/20 bg-amber-400/[0.08] text-amber-200"
              : actionTone === "info" || repairIsRunning
                ? "border-sky-400/20 bg-sky-400/[0.08] text-sky-200"
                : "border-emerald-400/20 bg-emerald-400/[0.08] text-emerald-200"
          }`}
          role="status"
        >
          {repairIsRunning ? (
            <RefreshIcon className="size-4 animate-spin" />
          ) : (
            <CheckIcon className="size-4" />
          )}
          {actionMessage}
        </div>
      ) : null}
      {actionError ? <ErrorNotice>{actionError}</ErrorNotice> : null}
      {loadError ? (
        <ErrorNotice onRetry={() => void loadDashboard()}>
          Could not load dashboard: {loadError}
        </ErrorNotice>
      ) : null}

      {!data && !loadError ? <LoadingBlock label="Loading dashboard…" /> : null}

      {data ? (
        <>
          {data.dry_run ? (
            <aside
              aria-label="Dry run status"
              className="rounded-xl border border-amber-400/25 bg-amber-400/[0.09] px-4 py-3 text-sm text-amber-100"
            >
              <p className="font-semibold">Dry run is enabled</p>
              <p className="mt-1 text-amber-200/80">
                Repair actions are simulations: no files are written and
                qBittorrent is not rechecked. Disable Dry run in Settings and
                save before performing a real repair.
              </p>
            </aside>
          ) : null}
          <section aria-label="Connector health" className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-zinc-200">
                Connector health
              </h2>
              <p className="text-xs text-zinc-600">Live service status</p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
              {data.connectors.map((connector) => (
                <ConnectorCard connector={connector} key={connector.id} />
              ))}
            </div>
          </section>

          <section aria-label="Repair funnel" className="space-y-3">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-zinc-200">
                  Lifetime repair funnel
                </h2>
                <p className="mt-1 text-xs text-zinc-600">
                  Each stage narrows from a CrowdNFO match to a verified seeding
                  torrent.
                </p>
              </div>
              <div className="flex gap-2 text-xs">
                <span className="rounded-full border border-amber-400/15 bg-amber-400/[0.07] px-2.5 py-1 text-amber-300">
                  {data.counters.misses.toLocaleString()} not found
                </span>
                <span className="rounded-full border border-violet-400/15 bg-violet-400/[0.07] px-2.5 py-1 text-violet-300">
                  {data.counters.uploaded.toLocaleString()} contributions
                </span>
              </div>
            </div>
            <div className="grid overflow-hidden rounded-2xl border border-white/[0.07] bg-zinc-900/70 shadow-panel sm:grid-cols-2 xl:grid-cols-4">
              {repairFunnel.map(([label, key, description], index) => (
                <div
                  aria-label={label}
                  className="relative border-b border-white/[0.06] px-5 py-5 sm:border-r sm:py-6 xl:border-b-0"
                  key={key}
                  role="group"
                >
                  <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-sky-500/80">
                    Stage {index + 1}
                  </p>
                  <p className="mt-2 text-3xl font-semibold tabular-nums text-white">
                    {data.counters[key].toLocaleString()}
                  </p>
                  <p className="mt-1 text-xs font-semibold text-zinc-300">
                    {label}
                  </p>
                  <p className="mt-1 text-[10px] leading-4 text-zinc-600">
                    {description}
                  </p>
                </div>
              ))}
            </div>
          </section>

          <div className="space-y-6">
            <Panel
              description="NFO-only repairs stay focused; unrelated incomplete downloads are separated below."
              title="Incomplete qBittorrent torrents"
            >
              <section
                aria-label="Incomplete qBittorrent torrents"
                className="overflow-hidden"
              >
                <p className="border-b border-white/[0.05] px-5 py-3 text-xs text-zinc-500">
                  {repairableTorrents.toLocaleString()} repairable ·{" "}
                  {data.stuck_torrents.length.toLocaleString()} incomplete
                </p>
                {data.stuck_torrents.length === 0 ? (
                  <p className="px-5 py-10 text-center text-sm text-zinc-500">
                    Nothing needs attention. No incomplete qBittorrent downloads
                    were found.
                  </p>
                ) : (
                  <div>
                    <div className="border-t border-white/[0.05] bg-sky-400/[0.025] px-5 py-3">
                      <h3 className="text-xs font-semibold uppercase tracking-[0.16em] text-sky-300">
                        NFO repair queue
                      </h3>
                      <p className="mt-1 text-[11px] text-zinc-600">
                        {nfoRepairTorrents.length.toLocaleString()} NFO-only
                        candidates
                      </p>
                    </div>
                    {nfoRepairTorrents.length ? (
                      nfoRepairTorrents.map((torrent) => (
                        <TorrentCard
                          dryRun={data.dry_run}
                          key={torrent.hash}
                          onRepair={() =>
                            void runAction(
                              `repair-${torrent.hash}`,
                              `/api/torrents/${encodeURIComponent(torrent.hash)}/repair`,
                            )
                          }
                          pending={pendingAction === `repair-${torrent.hash}`}
                          torrent={torrent}
                        />
                      ))
                    ) : (
                      <p className="border-t border-white/[0.05] px-5 py-6 text-sm text-zinc-600">
                        No NFO-only repair candidates right now.
                      </p>
                    )}

                    {otherIncompleteTorrents.length ? (
                      <>
                        <div className="border-t border-white/[0.05] bg-zinc-950/30 px-5 py-3">
                          <h3 className="text-xs font-semibold uppercase tracking-[0.16em] text-zinc-400">
                            Other incomplete downloads
                          </h3>
                          <p className="mt-1 text-[11px] text-zinc-600">
                            Visible for context; crowdarr will not modify these.
                          </p>
                        </div>
                        {otherIncompleteTorrents.map((torrent) => (
                          <TorrentCard
                            dryRun={data.dry_run}
                            key={torrent.hash}
                            onRepair={() => undefined}
                            pending={false}
                            torrent={torrent}
                          />
                        ))}
                      </>
                    ) : null}
                  </div>
                )}
              </section>
            </Panel>

            <Panel
              description="Newest repairs, matches, misses, and uploads."
              title="Recent activity"
            >
              <div
                aria-label="Recent activity"
                className="divide-y divide-white/[0.05]"
                role="feed"
              >
                {data.recent_activity.length === 0 ? (
                  <p className="px-5 py-10 text-center text-sm text-zinc-500">
                    Activity will appear after the first scan.
                  </p>
                ) : (
                  data.recent_activity.map((item, index) => (
                    <article
                      aria-posinset={index + 1}
                      aria-setsize={data.recent_activity.length}
                      className="flex gap-3 px-5 py-4"
                      key={item.id}
                    >
                      <span
                        className={`mt-2 size-2 shrink-0 rounded-full ${activityAccent(item)}`}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-start justify-between gap-3">
                          <h3 className="truncate text-sm font-medium text-zinc-200">
                            {item.title}
                          </h3>
                          <time
                            className="shrink-0 text-[11px] text-zinc-600"
                            dateTime={item.created_at}
                          >
                            {new Date(item.created_at).toLocaleTimeString([], {
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </time>
                        </div>
                        <p className="mt-1 text-xs leading-5 text-zinc-500">
                          {item.message}
                        </p>
                        {item.miss_id ? (
                          <Button
                            aria-label={`Retry ${item.title}`}
                            className="mt-2 min-h-8 px-2.5 py-1 text-xs"
                            disabled={pendingAction === `retry-${item.miss_id}`}
                            onClick={() =>
                              void runAction(
                                `retry-${item.miss_id}`,
                                `/api/actions/misses/${encodeURIComponent(item.miss_id!)}/retry`,
                              )
                            }
                            type="button"
                            variant="ghost"
                          >
                            <RefreshIcon className="size-3.5" />
                            Retry match
                          </Button>
                        ) : null}
                      </div>
                    </article>
                  ))
                )}
              </div>
            </Panel>
          </div>
        </>
      ) : null}
    </div>
  );
}
