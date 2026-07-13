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
} from "../types";

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Unexpected error";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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
            reason:
              typeof item.reason === "string"
                ? (item.reason as DashboardData["stuck_torrents"][number]["reason"])
                : repairable
                  ? "ready"
                  : "inspection_failed",
            repairable,
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
  return (
    <article className="rounded-xl border border-white/[0.06] bg-zinc-950/45 p-4 transition hover:border-white/10 hover:bg-zinc-950/70">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-zinc-100">
            {connector.name}
          </h3>
          <p className="mt-1 truncate text-xs text-zinc-500">
            {connector.message}
          </p>
        </div>
        <span
          aria-label={connector.status}
          className={`mt-1 size-2.5 shrink-0 rounded-full ${statusStyles[connector.status] ?? statusStyles.unknown} ${connector.status === "healthy" ? "shadow-[0_0_9px_rgb(52_211_153/0.7)]" : ""}`}
          role="img"
        />
      </div>
      <p className="mt-4 text-[11px] font-medium uppercase tracking-wider text-zinc-600">
        {connector.latency_ms == null
          ? connector.status
          : `${connector.latency_ms} ms latency`}
      </p>
    </article>
  );
}

const metricLabels = [
  ["NFOs fetched", "fetched", "Downloaded from CrowdNFO"],
  ["Torrents repaired", "repaired", "Verified complete and seeding"],
  ["Uploads completed", "uploaded", "Contributions accepted"],
  ["CrowdNFO matches", "matches", "Lookups that returned an NFO"],
  ["CrowdNFO misses", "misses", "Lookups without a usable NFO"],
] as const;

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
const JOB_POLL_ATTEMPTS = 310;

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

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData>();
  const [loadError, setLoadError] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [actionMessage, setActionMessage] = useState<string>();
  const [pendingAction, setPendingAction] = useState<string>();
  const repairIsRunning =
    pendingAction?.startsWith("repair-") &&
    actionMessage === "Repair is running…";
  const repairableTorrents =
    data?.stuck_torrents.filter((torrent) => torrent.repairable).length ?? 0;

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
    try {
      const response = await apiRequest<ActionResponse>(path, {
        method: "POST",
      });
      if (data?.dry_run) {
        setActionMessage(
          "Simulation queued — dry run will not write files and will not recheck qBittorrent.",
        );
      } else if (key.startsWith("repair-") && response.job_id) {
        setActionMessage("Repair is running…");
        const job = await waitForJob(response.job_id);
        if (job === undefined) {
          setActionMessage(
            "Repair is still running — check Recent activity for the result.",
          );
        } else {
          await loadDashboard();
          if (job.status === "success") {
            setActionMessage("Repair completed successfully.");
          } else if (job.status === "dry_run") {
            setActionMessage(
              "Repair finished as a dry-run simulation; no files were changed.",
            );
          } else {
            setActionMessage(undefined);
            setActionError(
              `Repair ${job.status} — see Recent activity for the exact reason.`,
            );
          }
        }
      } else {
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
            data?.dry_run
              ? "border-amber-400/20 bg-amber-400/[0.08] text-amber-200"
              : repairIsRunning
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

          <section
            aria-label="Lifetime counters"
            className="grid grid-cols-2 overflow-hidden rounded-2xl border border-white/[0.07] bg-zinc-900/70 shadow-panel sm:grid-cols-3 xl:grid-cols-5"
          >
            {metricLabels.map(([label, key, description]) => (
              <div
                aria-label={label}
                className="border-b border-r border-white/[0.06] px-5 py-5 last:border-r-0 sm:py-6"
                key={key}
                role="group"
              >
                <p className="text-2xl font-semibold tabular-nums text-white sm:text-3xl">
                  {data.counters[key].toLocaleString()}
                </p>
                <p className="mt-1 text-xs font-medium text-zinc-500">
                  {label}
                </p>
                <p className="mt-1 text-[10px] leading-4 text-zinc-700">
                  {description}
                </p>
              </div>
            ))}
          </section>

          <div className="space-y-6">
            <Panel
              description="Every incomplete qBittorrent download, with the exact reason it is or is not ready for NFO repair."
              title="Incomplete qBittorrent torrents"
            >
              <section
                aria-label="Incomplete qBittorrent torrents"
                className="overflow-x-auto"
              >
                <p className="border-b border-white/[0.05] px-5 py-3 text-xs text-zinc-500">
                  {repairableTorrents.toLocaleString()} repairable ·{" "}
                  {data.stuck_torrents.length.toLocaleString()} incomplete
                </p>
                {data.stuck_torrents.length === 0 ? (
                  <p className="px-5 py-10 text-center text-sm text-zinc-500">
                    No incomplete qBittorrent downloads were found.
                  </p>
                ) : (
                  <table className="w-full min-w-[700px] text-left text-sm">
                    <thead className="bg-white/[0.02] text-[11px] uppercase tracking-wider text-zinc-600">
                      <tr>
                        <th className="px-5 py-3 font-medium" scope="col">
                          Release
                        </th>
                        <th className="px-4 py-3 font-medium" scope="col">
                          Status
                        </th>
                        <th className="px-4 py-3 font-medium" scope="col">
                          NFO
                        </th>
                        <th className="px-4 py-3 font-medium" scope="col">
                          Progress
                        </th>
                        <th
                          className="px-5 py-3 text-right font-medium"
                          scope="col"
                        >
                          Action
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/[0.05]">
                      {data.stuck_torrents.map((torrent) => (
                        <tr
                          className="transition hover:bg-white/[0.025]"
                          key={torrent.hash}
                        >
                          <td className="max-w-64 px-5 py-4">
                            <p className="truncate font-medium text-zinc-200">
                              {torrent.name}
                            </p>
                            <p className="mt-1 text-xs text-zinc-600">
                              {torrent.category}
                            </p>
                          </td>
                          <td className="max-w-64 px-4 py-4 text-xs text-zinc-400">
                            <span className="block font-medium text-zinc-300">
                              {torrent.state}
                            </span>
                            <span className="mt-1 block text-zinc-600">
                              {reasonLabels[torrent.reason] ?? torrent.reason}
                            </span>
                          </td>
                          <td className="max-w-64 px-4 py-4 text-xs text-zinc-400">
                            {torrent.missing_nfo_count > 1 ? (
                              <>
                                <span className="block font-medium text-zinc-300">
                                  {torrent.missing_nfo_count} NFO files
                                </span>
                                <span className="mt-1 block truncate text-zinc-600">
                                  {torrent.missing_nfo_path}
                                </span>
                              </>
                            ) : torrent.missing_nfo_path ? (
                              <span className="block truncate">
                                {torrent.missing_nfo_path}
                              </span>
                            ) : (
                              <span className="text-zinc-700">—</span>
                            )}
                          </td>
                          <td className="px-4 py-4 font-mono text-xs text-amber-300">
                            {(torrent.progress * 100).toFixed(1)}%
                          </td>
                          <td className="px-5 py-4 text-right">
                            {torrent.repairable ? (
                              <Button
                                aria-label={`${data.dry_run ? "Simulate repair" : "Repair"} ${torrent.name}`}
                                disabled={
                                  pendingAction === `repair-${torrent.hash}`
                                }
                                onClick={() =>
                                  void runAction(
                                    `repair-${torrent.hash}`,
                                    `/api/torrents/${encodeURIComponent(torrent.hash)}/repair`,
                                  )
                                }
                                type="button"
                              >
                                <RepairIcon className="size-4" />
                                {data.dry_run ? "Simulate repair" : "Repair"}
                              </Button>
                            ) : (
                              <span className="text-xs text-zinc-600">
                                Not NFO-only
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
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
