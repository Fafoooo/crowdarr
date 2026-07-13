export type ConnectorState =
  "healthy" | "unhealthy" | "degraded" | "disabled" | "unknown";

export interface ConnectorHealth {
  id: string;
  latency_ms: number | null;
  message: string;
  name: string;
  status: ConnectorState;
}

export interface DashboardCounters {
  fetched: number;
  matches: number;
  misses: number;
  repaired: number;
  uploaded: number;
}

export interface ActivityItem {
  created_at: string;
  id: string;
  message: string;
  miss_id?: string;
  status: "success" | "warning" | "error" | "info";
  title: string;
  type: string;
}

export interface StuckTorrent {
  category: string;
  hash: string;
  missing_nfo_path: string;
  name: string;
  progress: number;
}

export interface DashboardData {
  connectors: ConnectorHealth[];
  counters: DashboardCounters;
  dry_run: boolean;
  recent_activity: ActivityItem[];
  stuck_torrents: StuckTorrent[];
}

export interface ActionResponse {
  job_id: string;
  message?: string;
  status?: string;
}

export interface JobResponse {
  job_id: string;
  kind: string;
  result: { detail?: string };
  status:
    | "queued"
    | "running"
    | "success"
    | "partial"
    | "failed"
    | "skipped"
    | "dry_run";
}

export type ConnectorId =
  | "crowdnfo"
  | "qbittorrent"
  | "sabnzbd"
  | "radarr"
  | "sonarr"
  | "umlautadaptarr";

export interface ConnectorSettings {
  api_key?: string;
  api_key_configured?: boolean;
  enabled: boolean;
  password?: string;
  password_configured?: boolean;
  url: string;
  username?: string;
}

export interface PathMapping {
  connector_path: string;
  local_path: string;
}

export interface CategoryMapping {
  category: string;
  crowdnfo_category: string;
}

export interface SettingsData {
  backfill_cron: string;
  category_mappings: CategoryMapping[];
  connectors: Record<Exclude<ConnectorId, "crowdnfo">, ConnectorSettings>;
  contribution: {
    enabled: boolean;
    filelist: boolean;
    mediainfo: boolean;
    nfo: boolean;
  };
  crowdnfo: {
    api_key?: string;
    api_key_configured: boolean;
    base_url: string;
  };
  download_mode: "off" | "new_only" | "new_and_backfill";
  dry_run: boolean;
  matching: {
    max_hash_size_gib: number;
    strategy: "hash_then_release_name" | "hash_only" | "release_name_only";
  };
  mismatch_policy: "keep" | "remove";
  path_mappings: PathMapping[];
  recheck_after_repair: boolean;
}

export interface ConnectorTestResponse {
  latency_ms: number | null;
  message: string;
  status: ConnectorState;
}

export type LogLevel = "debug" | "info" | "warning" | "error";

export interface LogEntry {
  context: Record<string, unknown>;
  event: string;
  id: string;
  level: LogLevel;
  message: string;
  timestamp: string;
}

export interface LogsResponse {
  items: LogEntry[];
  next_cursor: string | null;
}
