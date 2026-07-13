import {
  useCallback,
  useEffect,
  useState,
  type ChangeEvent,
  type FormEvent,
  type ReactNode,
} from "react";

import { apiRequest, ApiError } from "../api/client";
import { CheckIcon, PlusIcon, TrashIcon } from "../components/icons";
import {
  Button,
  ErrorNotice,
  LoadingBlock,
  PageHeader,
  inputClassName,
  labelClassName,
} from "../components/ui";
import type {
  CategoryMapping,
  ConnectorId,
  ConnectorSettings,
  ConnectorTestResponse,
  PathMapping,
  SettingsData,
} from "../types";

type SettingsDialect = "ui" | "canonical";
type ConfigurableConnectorId = Exclude<ConnectorId, "crowdnfo">;
type SecretKey =
  | "crowdnfo_api_key"
  | "qbittorrent_password"
  | "sabnzbd_api_key"
  | "radarr_api_key"
  | "sonarr_api_key";

type SecretDrafts = Record<SecretKey, string>;

const emptySecrets: SecretDrafts = {
  crowdnfo_api_key: "",
  qbittorrent_password: "",
  radarr_api_key: "",
  sabnzbd_api_key: "",
  sonarr_api_key: "",
};

const connectorDefinitions: Array<{
  id: ConfigurableConnectorId;
  label: string;
  secretKey?: SecretKey;
  secretLabel?: string;
  secretType?: "api_key" | "password";
  supportsUsername?: boolean;
}> = [
  {
    id: "qbittorrent",
    label: "qBittorrent",
    secretKey: "qbittorrent_password",
    secretLabel: "qBittorrent password",
    secretType: "password",
    supportsUsername: true,
  },
  {
    id: "sabnzbd",
    label: "SABnzbd",
    secretKey: "sabnzbd_api_key",
    secretLabel: "SABnzbd API key",
    secretType: "api_key",
  },
  {
    id: "radarr",
    label: "Radarr",
    secretKey: "radarr_api_key",
    secretLabel: "Radarr API key",
    secretType: "api_key",
  },
  {
    id: "sonarr",
    label: "Sonarr",
    secretKey: "sonarr_api_key",
    secretLabel: "Sonarr API key",
    secretType: "api_key",
  },
  { id: "umlautadaptarr", label: "UmlautAdaptarr" },
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function booleanValue(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function sanitiseConnector(
  value: unknown,
  configuredSecrets: Record<string, unknown>,
  id: ConfigurableConnectorId,
): ConnectorSettings {
  const connector = record(value);
  const apiKeyConfigured = booleanValue(
    connector.api_key_configured,
    booleanValue(configuredSecrets[`${id}_api_key`]),
  );
  const passwordConfigured = booleanValue(
    connector.password_configured,
    booleanValue(configuredSecrets[`${id}_password`]),
  );

  return {
    api_key_configured: apiKeyConfigured,
    enabled: booleanValue(connector.enabled),
    password_configured: passwordConfigured,
    url: stringValue(connector.url, stringValue(connector.base_url)),
    username: stringValue(connector.username),
  };
}

function normalisePathMappings(value: unknown): PathMapping[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const mapping = record(entry);
    return {
      connector_path: stringValue(
        mapping.connector_path,
        stringValue(mapping.remote_root),
      ),
      local_path: stringValue(
        mapping.local_path,
        stringValue(mapping.local_root),
      ),
    };
  });
}

function normaliseCategoryMappings(value: unknown): CategoryMapping[] {
  if (Array.isArray(value)) {
    return value.map((entry) => {
      const mapping = record(entry);
      return {
        category: stringValue(mapping.category),
        save_path: stringValue(mapping.save_path),
      };
    });
  }
  if (isRecord(value)) {
    return Object.entries(value).map(([category, savePath]) => ({
      category,
      save_path: stringValue(savePath),
    }));
  }
  return [];
}

function normaliseSettings(raw: unknown): {
  dialect: SettingsDialect;
  settings: SettingsData;
} {
  const value = record(raw);
  const nestedConnectors = isRecord(value.connectors);
  const connectors = nestedConnectors ? record(value.connectors) : value;
  const configuredSecrets = record(value.secrets_configured);
  const crowdnfo = record(value.crowdnfo);
  const contribution = record(value.contribution ?? value.contribute);
  const matching = record(value.matching);
  const hashBytes = numberValue(value.hash_max_size_bytes, 80 * 1024 ** 3);

  return {
    dialect: nestedConnectors ? "ui" : "canonical",
    settings: {
      backfill_cron: stringValue(value.backfill_cron, "0 3 * * *"),
      category_mappings: normaliseCategoryMappings(value.category_mappings),
      connectors: {
        qbittorrent: sanitiseConnector(
          connectors.qbittorrent,
          configuredSecrets,
          "qbittorrent",
        ),
        radarr: sanitiseConnector(
          connectors.radarr,
          configuredSecrets,
          "radarr",
        ),
        sabnzbd: sanitiseConnector(
          connectors.sabnzbd,
          configuredSecrets,
          "sabnzbd",
        ),
        sonarr: sanitiseConnector(
          connectors.sonarr,
          configuredSecrets,
          "sonarr",
        ),
        umlautadaptarr: sanitiseConnector(
          connectors.umlautadaptarr,
          configuredSecrets,
          "umlautadaptarr",
        ),
      },
      contribution: {
        enabled: booleanValue(contribution.enabled),
        filelist: booleanValue(contribution.filelist),
        mediainfo: booleanValue(contribution.mediainfo),
        nfo: booleanValue(contribution.nfo),
      },
      crowdnfo: {
        api_key_configured: booleanValue(
          crowdnfo.api_key_configured,
          booleanValue(configuredSecrets.crowdnfo_api_key),
        ),
        base_url: stringValue(crowdnfo.base_url, "https://crowdnfo.net"),
      },
      download_mode:
        value.download_mode === "new_only" ||
        value.download_mode === "new_and_backfill"
          ? value.download_mode
          : "off",
      dry_run: booleanValue(value.dry_run, true),
      matching: {
        max_hash_size_gib: numberValue(
          matching.max_hash_size_gib,
          Math.round(hashBytes / 1024 ** 3),
        ),
        strategy:
          matching.strategy === "hash_only" ||
          matching.strategy === "release_name_only"
            ? matching.strategy
            : value.match_strategy === "hash_only" ||
                value.match_strategy === "release_name_only"
              ? value.match_strategy
              : "hash_then_release_name",
      },
      mismatch_policy:
        value.mismatch_policy === "remove" ||
        value.nfo_mismatch_policy === "remove"
          ? "remove"
          : "keep",
      path_mappings: normalisePathMappings(value.path_mappings),
      recheck_after_repair: booleanValue(
        value.recheck_after_repair,
        booleanValue(value.auto_recheck, true),
      ),
    },
  };
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Unexpected error";
}

function Field({
  children,
  id,
  label,
}: {
  children: ReactNode;
  id: string;
  label: string;
}) {
  return (
    <div>
      <label className={labelClassName} htmlFor={id}>
        {label}
      </label>
      {children}
    </div>
  );
}

function Toggle({
  checked,
  children,
  disabled,
  onChange,
}: {
  checked: boolean;
  children: ReactNode;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 rounded-lg border border-white/[0.06] bg-zinc-950/35 px-3 py-2.5 text-sm text-zinc-300 transition hover:border-white/10">
      <span>{children}</span>
      <input
        checked={checked}
        className="size-4 shrink-0 accent-sky-400"
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        type="checkbox"
      />
    </label>
  );
}

function SecretInput({
  configured,
  id,
  label,
  onChange,
  value,
}: {
  configured: boolean;
  id: string;
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <Field id={id} label={label}>
      <input
        autoComplete="new-password"
        className={inputClassName}
        id={id}
        onChange={(event) => onChange(event.target.value)}
        placeholder={
          configured ? "Configured — leave blank to keep" : "Not configured"
        }
        type="password"
        value={value}
      />
    </Field>
  );
}

function ConnectorFieldset({
  connector,
  definition,
  onSecretChange,
  onTest,
  onUpdate,
  pending,
  secretValue,
}: {
  connector: ConnectorSettings;
  definition: (typeof connectorDefinitions)[number];
  onSecretChange: (key: SecretKey, value: string) => void;
  onTest: (id: ConnectorId, label: string) => void;
  onUpdate: (
    id: ConfigurableConnectorId,
    patch: Partial<ConnectorSettings>,
  ) => void;
  pending: boolean;
  secretValue?: string;
}) {
  const { id, label, secretKey, secretLabel, secretType, supportsUsername } =
    definition;
  const configured =
    secretType === "password"
      ? Boolean(connector.password_configured)
      : Boolean(connector.api_key_configured);

  return (
    <fieldset className="rounded-2xl border border-white/[0.07] bg-zinc-900/70 p-5 shadow-panel">
      <legend className="sr-only">{label}</legend>
      <div className="mb-5 flex items-center justify-between gap-4 border-b border-white/[0.06] pb-4">
        <div>
          <h2 className="font-semibold text-zinc-100">{label}</h2>
          <p className="mt-1 text-xs text-zinc-500">
            {connector.enabled ? "Enabled" : "Optional connector"}
          </p>
        </div>
        <Button
          aria-label={`Test ${label} connection`}
          disabled={pending}
          onClick={() => onTest(id, label)}
          type="button"
        >
          {pending ? "Testing…" : "Test"}
        </Button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Toggle
          checked={connector.enabled}
          onChange={(enabled) => onUpdate(id, { enabled })}
        >
          Enable {label}
        </Toggle>
        <div className="hidden sm:block" />
        <Field id={`${id}-url`} label="URL">
          <input
            className={inputClassName}
            id={`${id}-url`}
            onChange={(event) => onUpdate(id, { url: event.target.value })}
            placeholder={`http://${id}:port`}
            type="url"
            value={connector.url}
          />
        </Field>
        {supportsUsername ? (
          <Field id={`${id}-username`} label="Username">
            <input
              autoComplete="username"
              className={inputClassName}
              id={`${id}-username`}
              onChange={(event) =>
                onUpdate(id, { username: event.target.value })
              }
              type="text"
              value={connector.username ?? ""}
            />
          </Field>
        ) : (
          <div className="hidden sm:block" />
        )}
        {secretKey && secretLabel ? (
          <SecretInput
            configured={configured}
            id={`${id}-secret`}
            label={secretLabel}
            onChange={(value) => onSecretChange(secretKey, value)}
            value={secretValue ?? ""}
          />
        ) : null}
      </div>
    </fieldset>
  );
}

function buildPayload(
  settings: SettingsData,
  secrets: SecretDrafts,
  dialect: SettingsDialect,
): Record<string, unknown> {
  if (dialect === "ui") {
    const payload: Record<string, unknown> = {
      ...settings,
      connectors: {
        ...settings.connectors,
        qbittorrent: {
          ...settings.connectors.qbittorrent,
          ...(secrets.qbittorrent_password
            ? { password: secrets.qbittorrent_password }
            : {}),
        },
        radarr: {
          ...settings.connectors.radarr,
          ...(secrets.radarr_api_key
            ? { api_key: secrets.radarr_api_key }
            : {}),
        },
        sabnzbd: {
          ...settings.connectors.sabnzbd,
          ...(secrets.sabnzbd_api_key
            ? { api_key: secrets.sabnzbd_api_key }
            : {}),
        },
        sonarr: {
          ...settings.connectors.sonarr,
          ...(secrets.sonarr_api_key
            ? { api_key: secrets.sonarr_api_key }
            : {}),
        },
      },
      crowdnfo: {
        ...settings.crowdnfo,
        ...(secrets.crowdnfo_api_key
          ? { api_key: secrets.crowdnfo_api_key }
          : {}),
      },
    };
    return payload;
  }

  const connectorPayload = (
    connector: ConnectorSettings,
    secret?: { field: "api_key" | "password"; value: string },
  ) => ({
    enabled: connector.enabled,
    base_url: connector.url,
    ...(connector.username ? { username: connector.username } : {}),
    ...(secret?.value ? { [secret.field]: secret.value } : {}),
  });

  return {
    auto_recheck: settings.recheck_after_repair,
    backfill_cron: settings.backfill_cron,
    category_mappings: Object.fromEntries(
      settings.category_mappings.map(({ category, save_path }) => [
        category,
        save_path,
      ]),
    ),
    contribute: settings.contribution,
    crowdnfo: {
      base_url: settings.crowdnfo.base_url,
      ...(secrets.crowdnfo_api_key
        ? { api_key: secrets.crowdnfo_api_key }
        : {}),
    },
    download_mode: settings.download_mode,
    dry_run: settings.dry_run,
    hash_max_size_bytes: Math.round(
      settings.matching.max_hash_size_gib * 1024 ** 3,
    ),
    match_strategy: settings.matching.strategy,
    nfo_mismatch_policy: settings.mismatch_policy,
    path_mappings: settings.path_mappings.map(
      ({ connector_path, local_path }) => ({
        local_root: local_path,
        remote_root: connector_path,
      }),
    ),
    qbittorrent: connectorPayload(settings.connectors.qbittorrent, {
      field: "password",
      value: secrets.qbittorrent_password,
    }),
    radarr: connectorPayload(settings.connectors.radarr, {
      field: "api_key",
      value: secrets.radarr_api_key,
    }),
    sabnzbd: connectorPayload(settings.connectors.sabnzbd, {
      field: "api_key",
      value: secrets.sabnzbd_api_key,
    }),
    sonarr: connectorPayload(settings.connectors.sonarr, {
      field: "api_key",
      value: secrets.sonarr_api_key,
    }),
    umlautadaptarr: connectorPayload(settings.connectors.umlautadaptarr),
  };
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<SettingsData>();
  const [dialect, setDialect] = useState<SettingsDialect>("ui");
  const [secrets, setSecrets] = useState<SecretDrafts>(emptySecrets);
  const [loadError, setLoadError] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [feedback, setFeedback] = useState<string>();
  const [pendingTest, setPendingTest] = useState<ConnectorId>();
  const [saving, setSaving] = useState(false);

  const loadSettings = useCallback(async () => {
    setLoadError(undefined);
    try {
      const result = normaliseSettings(
        await apiRequest<unknown>("/api/settings"),
      );
      setSettings(result.settings);
      setDialect(result.dialect);
      setSecrets(emptySecrets);
    } catch (error) {
      setLoadError(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const update = (recipe: (current: SettingsData) => SettingsData) => {
    setSettings((current) => (current ? recipe(current) : current));
  };

  const updateConnector = (
    id: ConfigurableConnectorId,
    patch: Partial<ConnectorSettings>,
  ) => {
    update((current) => ({
      ...current,
      connectors: {
        ...current.connectors,
        [id]: { ...current.connectors[id], ...patch },
      },
    }));
  };

  const testConnector = async (id: ConnectorId, label: string) => {
    setPendingTest(id);
    setActionError(undefined);
    setFeedback(undefined);
    try {
      const response = await apiRequest<ConnectorTestResponse>(
        `/api/connectors/${id}/test`,
        { method: "POST" },
      );
      setFeedback(
        `${response.message}${response.latency_ms == null ? "" : ` · ${response.latency_ms} ms`}`,
      );
    } catch (error) {
      setActionError(`${label}: ${errorMessage(error)}`);
    } finally {
      setPendingTest(undefined);
    }
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!settings) return;
    setSaving(true);
    setActionError(undefined);
    setFeedback(undefined);
    try {
      const response = await apiRequest<unknown>("/api/settings", {
        body: JSON.stringify(buildPayload(settings, secrets, dialect)),
        method: "PUT",
      });
      const result = normaliseSettings(response);
      setSettings(result.settings);
      setDialect(result.dialect);
      setSecrets(emptySecrets);
      setFeedback("Settings saved");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const updatePathMapping = (index: number, patch: Partial<PathMapping>) => {
    update((current) => ({
      ...current,
      path_mappings: current.path_mappings.map((mapping, mappingIndex) =>
        mappingIndex === index ? { ...mapping, ...patch } : mapping,
      ),
    }));
  };

  const updateCategoryMapping = (
    index: number,
    patch: Partial<CategoryMapping>,
  ) => {
    update((current) => ({
      ...current,
      category_mappings: current.category_mappings.map(
        (mapping, mappingIndex) =>
          mappingIndex === index ? { ...mapping, ...patch } : mapping,
      ),
    }));
  };

  return (
    <div className="space-y-7">
      <PageHeader
        description="Configure every integration, matching strategy, and safety boundary from one place. Secrets are write-only."
        eyebrow="Configuration"
        title="Settings"
      />

      {feedback ? (
        <div
          className="flex items-center gap-2 rounded-xl border border-emerald-400/20 bg-emerald-400/[0.08] px-4 py-3 text-sm text-emerald-200"
          role="status"
        >
          <CheckIcon className="size-4" />
          {feedback}
        </div>
      ) : null}
      {actionError ? <ErrorNotice>{actionError}</ErrorNotice> : null}
      {loadError ? (
        <ErrorNotice onRetry={() => void loadSettings()}>
          Could not load settings: {loadError}
        </ErrorNotice>
      ) : null}
      {!settings && !loadError ? (
        <LoadingBlock label="Loading settings…" />
      ) : null}

      {settings ? (
        <form className="space-y-7" onSubmit={(event) => void save(event)}>
          <section aria-labelledby="connectors-heading" className="space-y-4">
            <div>
              <h2
                className="text-lg font-semibold text-zinc-100"
                id="connectors-heading"
              >
                Connections
              </h2>
              <p className="mt-1 text-sm text-zinc-500">
                Services are optional and can be tested before saving.
              </p>
            </div>

            <div className="grid gap-4 xl:grid-cols-2">
              <fieldset className="rounded-2xl border border-white/[0.07] bg-zinc-900/70 p-5 shadow-panel">
                <legend className="sr-only">CrowdNFO</legend>
                <div className="mb-5 flex items-center justify-between gap-4 border-b border-white/[0.06] pb-4">
                  <div>
                    <h2 className="font-semibold text-zinc-100">CrowdNFO</h2>
                    <p className="mt-1 text-xs text-zinc-500">
                      Community release database
                    </p>
                  </div>
                  <Button
                    aria-label="Test CrowdNFO connection"
                    disabled={pendingTest === "crowdnfo"}
                    onClick={() => void testConnector("crowdnfo", "CrowdNFO")}
                    type="button"
                  >
                    {pendingTest === "crowdnfo" ? "Testing…" : "Test"}
                  </Button>
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field id="crowdnfo-base-url" label="Base URL">
                    <input
                      className={inputClassName}
                      id="crowdnfo-base-url"
                      onChange={(event) =>
                        update((current) => ({
                          ...current,
                          crowdnfo: {
                            ...current.crowdnfo,
                            base_url: event.target.value,
                          },
                        }))
                      }
                      type="url"
                      value={settings.crowdnfo.base_url}
                    />
                  </Field>
                  <SecretInput
                    configured={settings.crowdnfo.api_key_configured}
                    id="crowdnfo-api-key"
                    label="CrowdNFO API key"
                    onChange={(value) =>
                      setSecrets((current) => ({
                        ...current,
                        crowdnfo_api_key: value,
                      }))
                    }
                    value={secrets.crowdnfo_api_key}
                  />
                </div>
              </fieldset>

              {connectorDefinitions.map((definition) => (
                <ConnectorFieldset
                  connector={settings.connectors[definition.id]}
                  definition={definition}
                  key={definition.id}
                  onSecretChange={(key, value) =>
                    setSecrets((current) => ({ ...current, [key]: value }))
                  }
                  onTest={(id, label) => void testConnector(id, label)}
                  onUpdate={updateConnector}
                  pending={pendingTest === definition.id}
                  secretValue={
                    definition.secretKey
                      ? secrets[definition.secretKey]
                      : undefined
                  }
                />
              ))}
            </div>
          </section>

          <section className="grid gap-4 xl:grid-cols-2">
            <div className="rounded-2xl border border-white/[0.07] bg-zinc-900/70 p-5 shadow-panel">
              <h2 className="font-semibold text-zinc-100">Automation modes</h2>
              <p className="mt-1 text-xs text-zinc-500">
                Choose when Crowdarrr downloads and contributes NFO data.
              </p>
              <div className="mt-5 space-y-4">
                <Field id="download-mode" label="Download mode">
                  <select
                    className={inputClassName}
                    id="download-mode"
                    onChange={(event: ChangeEvent<HTMLSelectElement>) =>
                      update((current) => ({
                        ...current,
                        download_mode: event.target
                          .value as SettingsData["download_mode"],
                      }))
                    }
                    value={settings.download_mode}
                  >
                    <option value="off">Off</option>
                    <option value="new_only">New downloads only</option>
                    <option value="new_and_backfill">
                      New downloads + backfill
                    </option>
                  </select>
                </Field>
                <Toggle
                  checked={settings.recheck_after_repair}
                  onChange={(recheck_after_repair) =>
                    update((current) => ({ ...current, recheck_after_repair }))
                  }
                >
                  Recheck after placing NFO
                </Toggle>
                <Toggle
                  checked={settings.contribution.enabled}
                  onChange={(enabled) =>
                    update((current) => ({
                      ...current,
                      contribution: { ...current.contribution, enabled },
                    }))
                  }
                >
                  Contribute to CrowdNFO
                </Toggle>
                <div className="grid gap-2 pl-3 sm:grid-cols-3">
                  {(
                    [
                      ["nfo", "Upload NFO"],
                      ["mediainfo", "Upload MediaInfo"],
                      ["filelist", "Upload file list"],
                    ] as const
                  ).map(([key, label]) => (
                    <Toggle
                      checked={settings.contribution[key]}
                      key={key}
                      onChange={(checked) =>
                        update((current) => ({
                          ...current,
                          contribution: {
                            ...current.contribution,
                            [key]: checked,
                          },
                        }))
                      }
                    >
                      {label}
                    </Toggle>
                  ))}
                </div>
              </div>
            </div>

            <div className="rounded-2xl border border-white/[0.07] bg-zinc-900/70 p-5 shadow-panel">
              <h2 className="font-semibold text-zinc-100">
                Matching &amp; safety
              </h2>
              <p className="mt-1 text-xs text-zinc-500">
                Bound expensive hashes and define mismatch behavior.
              </p>
              <div className="mt-5 grid gap-4 sm:grid-cols-2">
                <Field id="match-strategy" label="Match strategy">
                  <select
                    className={inputClassName}
                    id="match-strategy"
                    onChange={(event) =>
                      update((current) => ({
                        ...current,
                        matching: {
                          ...current.matching,
                          strategy: event.target
                            .value as SettingsData["matching"]["strategy"],
                        },
                      }))
                    }
                    value={settings.matching.strategy}
                  >
                    <option value="hash_then_release_name">
                      Hash, then release name
                    </option>
                    <option value="hash_only">Hash only</option>
                    <option value="release_name_only">Release name only</option>
                  </select>
                </Field>
                <Field id="maximum-hash-size" label="Maximum hash size (GiB)">
                  <input
                    className={inputClassName}
                    id="maximum-hash-size"
                    min="1"
                    onChange={(event) =>
                      update((current) => ({
                        ...current,
                        matching: {
                          ...current.matching,
                          max_hash_size_gib: Number(event.target.value),
                        },
                      }))
                    }
                    type="number"
                    value={settings.matching.max_hash_size_gib}
                  />
                </Field>
                <Field id="backfill-schedule" label="Backfill schedule (cron)">
                  <input
                    className={inputClassName}
                    id="backfill-schedule"
                    onChange={(event) =>
                      update((current) => ({
                        ...current,
                        backfill_cron: event.target.value,
                      }))
                    }
                    spellCheck="false"
                    type="text"
                    value={settings.backfill_cron}
                  />
                </Field>
                <div className="flex items-end">
                  <Toggle
                    checked={settings.dry_run}
                    onChange={(dry_run) =>
                      update((current) => ({ ...current, dry_run }))
                    }
                  >
                    Dry run
                  </Toggle>
                </div>
                <fieldset className="sm:col-span-2">
                  <legend className={labelClassName}>
                    NFO mismatch policy
                  </legend>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <label className="flex items-center gap-2 rounded-lg border border-white/[0.06] bg-zinc-950/35 px-3 py-2.5 text-sm text-zinc-300">
                      <input
                        checked={settings.mismatch_policy === "keep"}
                        className="accent-sky-400"
                        name="mismatch-policy"
                        onChange={() =>
                          update((current) => ({
                            ...current,
                            mismatch_policy: "keep",
                          }))
                        }
                        type="radio"
                      />
                      Keep mismatched NFO
                    </label>
                    <label className="flex items-center gap-2 rounded-lg border border-white/[0.06] bg-zinc-950/35 px-3 py-2.5 text-sm text-zinc-300">
                      <input
                        checked={settings.mismatch_policy === "remove"}
                        className="accent-sky-400"
                        name="mismatch-policy"
                        onChange={() =>
                          update((current) => ({
                            ...current,
                            mismatch_policy: "remove",
                          }))
                        }
                        type="radio"
                      />
                      Remove mismatched NFO
                    </label>
                  </div>
                </fieldset>
              </div>
            </div>
          </section>

          <section className="grid gap-4 xl:grid-cols-2">
            <div className="rounded-2xl border border-white/[0.07] bg-zinc-900/70 p-5 shadow-panel">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="font-semibold text-zinc-100">Path mappings</h2>
                  <p className="mt-1 text-xs text-zinc-500">
                    Translate connector paths into Crowdarrr-visible paths.
                  </p>
                </div>
                <Button
                  onClick={() =>
                    update((current) => ({
                      ...current,
                      path_mappings: [
                        ...current.path_mappings,
                        { connector_path: "", local_path: "" },
                      ],
                    }))
                  }
                  type="button"
                >
                  <PlusIcon className="size-4" />
                  Add path mapping
                </Button>
              </div>
              <div className="mt-5 space-y-3">
                {settings.path_mappings.map((mapping, index) => (
                  <div
                    className="grid gap-3 rounded-xl border border-white/[0.06] bg-zinc-950/35 p-3 sm:grid-cols-[1fr_1fr_auto]"
                    key={`path-${index}`}
                  >
                    <Field
                      id={`connector-path-${index}`}
                      label={`Connector path ${index + 1}`}
                    >
                      <input
                        className={inputClassName}
                        id={`connector-path-${index}`}
                        onChange={(event) =>
                          updatePathMapping(index, {
                            connector_path: event.target.value,
                          })
                        }
                        placeholder="/downloads"
                        type="text"
                        value={mapping.connector_path}
                      />
                    </Field>
                    <Field
                      id={`local-path-${index}`}
                      label={`Crowdarrr path ${index + 1}`}
                    >
                      <input
                        className={inputClassName}
                        id={`local-path-${index}`}
                        onChange={(event) =>
                          updatePathMapping(index, {
                            local_path: event.target.value,
                          })
                        }
                        placeholder="/data/downloads"
                        type="text"
                        value={mapping.local_path}
                      />
                    </Field>
                    <Button
                      aria-label={`Remove path mapping ${index + 1}`}
                      className="self-end px-3"
                      onClick={() =>
                        update((current) => ({
                          ...current,
                          path_mappings: current.path_mappings.filter(
                            (_, mappingIndex) => mappingIndex !== index,
                          ),
                        }))
                      }
                      type="button"
                      variant="ghost"
                    >
                      <TrashIcon className="size-4" />
                    </Button>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-2xl border border-white/[0.07] bg-zinc-900/70 p-5 shadow-panel">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="font-semibold text-zinc-100">
                    Category mappings
                  </h2>
                  <p className="mt-1 text-xs text-zinc-500">
                    Associate download categories with their save paths.
                  </p>
                </div>
                <Button
                  onClick={() =>
                    update((current) => ({
                      ...current,
                      category_mappings: [
                        ...current.category_mappings,
                        { category: "", save_path: "" },
                      ],
                    }))
                  }
                  type="button"
                >
                  <PlusIcon className="size-4" />
                  Add category mapping
                </Button>
              </div>
              <div className="mt-5 space-y-3">
                {settings.category_mappings.map((mapping, index) => (
                  <div
                    className="grid gap-3 rounded-xl border border-white/[0.06] bg-zinc-950/35 p-3 sm:grid-cols-[0.7fr_1.3fr_auto]"
                    key={`category-${index}`}
                  >
                    <Field
                      id={`category-${index}`}
                      label={`Category ${index + 1}`}
                    >
                      <input
                        className={inputClassName}
                        id={`category-${index}`}
                        onChange={(event) =>
                          updateCategoryMapping(index, {
                            category: event.target.value,
                          })
                        }
                        type="text"
                        value={mapping.category}
                      />
                    </Field>
                    <Field
                      id={`save-path-${index}`}
                      label={`Save path ${index + 1}`}
                    >
                      <input
                        className={inputClassName}
                        id={`save-path-${index}`}
                        onChange={(event) =>
                          updateCategoryMapping(index, {
                            save_path: event.target.value,
                          })
                        }
                        type="text"
                        value={mapping.save_path}
                      />
                    </Field>
                    <Button
                      aria-label={`Remove category mapping ${index + 1}`}
                      className="self-end px-3"
                      onClick={() =>
                        update((current) => ({
                          ...current,
                          category_mappings: current.category_mappings.filter(
                            (_, mappingIndex) => mappingIndex !== index,
                          ),
                        }))
                      }
                      type="button"
                      variant="ghost"
                    >
                      <TrashIcon className="size-4" />
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <div className="sticky bottom-4 flex items-center justify-end rounded-2xl border border-white/10 bg-zinc-950/90 p-3 shadow-2xl backdrop-blur-xl">
            <Button disabled={saving} type="submit" variant="primary">
              <CheckIcon className="size-4" />
              {saving ? "Saving…" : "Save settings"}
            </Button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
