import { useCallback, useEffect, useMemo, useState } from "react";

import { apiRequest, ApiError } from "../api/client";
import { RefreshIcon } from "../components/icons";
import {
  Button,
  ErrorNotice,
  LoadingBlock,
  PageHeader,
  Panel,
  inputClassName,
  labelClassName,
} from "../components/ui";
import type { LogEntry, LogLevel, LogsResponse } from "../types";

type LevelFilter = "all" | LogLevel;

const levelRank: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warning: 2,
  error: 3,
};

const levelStyles: Record<LogLevel, string> = {
  debug: "border-zinc-500/20 bg-zinc-500/10 text-zinc-400",
  error: "border-red-400/20 bg-red-400/10 text-red-300",
  info: "border-sky-400/20 bg-sky-400/10 text-sky-300",
  warning: "border-amber-400/20 bg-amber-400/10 text-amber-300",
};

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Unexpected error";
}

function Context({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  if (entries.length === 0) return null;

  return (
    <dl className="mt-3 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-zinc-600">
      {entries.map(([key, entry]) => (
        <div className="flex gap-1" key={key}>
          <dt>{key}=</dt>
          <dd className="max-w-80 truncate text-zinc-500">
            {typeof entry === "string" ? entry : JSON.stringify(entry)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function LogRow({ entry }: { entry: LogEntry }) {
  return (
    <article className="grid gap-3 border-b border-white/[0.05] px-5 py-4 transition last:border-b-0 hover:bg-white/[0.02] md:grid-cols-[130px_92px_minmax(0,1fr)]">
      <time
        className="font-mono text-xs tabular-nums text-zinc-600"
        dateTime={entry.timestamp}
      >
        {new Date(entry.timestamp).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        })}
      </time>
      <div>
        <span
          className={`inline-flex rounded-md border px-2 py-1 text-[10px] font-bold tracking-wider ${levelStyles[entry.level]}`}
        >
          {entry.level.toUpperCase()}
        </span>
      </div>
      <div className="min-w-0">
        <div className="flex flex-col gap-1 sm:flex-row sm:items-baseline sm:gap-3">
          <code className="shrink-0 text-xs font-semibold text-zinc-400">
            {entry.event}
          </code>
          <p className="text-sm leading-5 text-zinc-200">{entry.message}</p>
        </div>
        <Context value={entry.context} />
      </div>
    </article>
  );
}

export default function LogsPage() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [level, setLevel] = useState<LevelFilter>("all");
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string>();

  const loadLogs = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    setError(undefined);
    try {
      const response = await apiRequest<LogsResponse>("/api/logs?limit=200");
      setEntries(response.items);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  useEffect(() => {
    if (paused) return;
    const timer = window.setInterval(() => void loadLogs(), 5_000);
    return () => window.clearInterval(timer);
  }, [loadLogs, paused]);

  const visibleEntries = useMemo(() => {
    if (level === "all") return entries;
    return entries.filter(
      (entry) => levelRank[entry.level] >= levelRank[level],
    );
  }, [entries, level]);

  return (
    <div className="space-y-7">
      <PageHeader
        actions={
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => setPaused((current) => !current)}
              type="button"
            >
              <span
                className={`size-2 rounded-full ${paused ? "bg-amber-400" : "animate-pulse bg-emerald-400"}`}
              />
              {paused ? "Resume live updates" : "Pause live updates"}
            </Button>
            <Button
              disabled={refreshing}
              onClick={() => void loadLogs(true)}
              type="button"
              variant="primary"
            >
              <RefreshIcon className={refreshing ? "animate-spin" : ""} />
              Refresh logs
            </Button>
          </div>
        }
        description="Follow repairs, connector health, matches, and contribution events as they happen."
        eyebrow="Observability"
        title="Live logs"
      />

      {error ? <ErrorNotice>{error}</ErrorNotice> : null}

      <Panel
        actions={
          <div className="min-w-40">
            <label className={labelClassName} htmlFor="log-level">
              Log level
            </label>
            <select
              className={inputClassName}
              id="log-level"
              onChange={(event) => setLevel(event.target.value as LevelFilter)}
              value={level}
            >
              <option value="all">All levels</option>
              <option value="debug">Debug and above</option>
              <option value="info">Info and above</option>
              <option value="warning">Warning and above</option>
              <option value="error">Errors only</option>
            </select>
          </div>
        }
        description={`${entries.length} recent entries · ${paused ? "updates paused" : "refreshing every 5 seconds"}`}
        title="Event stream"
      >
        <div
          aria-label="Live log stream"
          aria-live={paused ? "off" : "polite"}
          className="min-h-64"
          role="log"
        >
          {loading && entries.length === 0 ? (
            <LoadingBlock label="Loading logs…" />
          ) : visibleEntries.length === 0 ? (
            <div className="grid min-h-64 place-items-center px-5 py-12 text-center">
              <div>
                <p className="text-sm font-medium text-zinc-300">
                  No matching entries
                </p>
                <p className="mt-1 text-xs text-zinc-600">
                  Change the severity filter or wait for new activity.
                </p>
              </div>
            </div>
          ) : (
            visibleEntries.map((entry) => (
              <LogRow entry={entry} key={entry.id} />
            ))
          )}
        </div>
      </Panel>
    </div>
  );
}
