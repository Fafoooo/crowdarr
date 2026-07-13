import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import App from "./App";
import {
  installFetchMock,
  jsonResponse,
  type MockRequest,
  visit,
} from "./test/mockFetch";

const crowdNfoCategories = [
  "Movies",
  "TV",
  "Games",
  "Software",
  "Music",
  "Books",
  "Audiobooks",
  "Other",
  "Unknown",
] as const;

// Mirrors SettingsStore.public_view(): connectors are top-level, secrets are
// represented only by write-only configuration flags, and patch names are the
// canonical backend names.
const settingsPublicView = {
  auto_recheck: true,
  backfill_cron: "0 3 * * *",
  category_mappings: { radarr: "Movies" },
  contribute: {
    enabled: true,
    filelist: false,
    mediainfo: true,
    nfo: true,
  },
  crowdnfo: {
    base_url: "https://crowdnfo.net/",
  },
  download_mode: "new_and_backfill",
  dry_run: true,
  hash_max_size_bytes: 80 * 1024 ** 3,
  match_strategy: "hash_then_release_name",
  nfo_mismatch_policy: "keep",
  path_mappings: [{ local_root: "/data", remote_root: "/data" }],
  qbittorrent: {
    base_url: "http://qbittorrent:8080/",
    enabled: true,
    username: "crowdarrr",
  },
  radarr: {
    base_url: "http://radarr:7878/",
    enabled: true,
    username: null,
  },
  sabnzbd: {
    base_url: "http://sabnzbd:8080/",
    enabled: false,
    username: null,
  },
  secrets_configured: {
    application_api_token: false,
    crowdnfo_api_key: true,
    qbittorrent_password: true,
    radarr_api_key: true,
    sabnzbd_api_key: true,
    sonarr_api_key: true,
  },
  sonarr: {
    base_url: "http://sonarr:8989/",
    enabled: true,
    username: null,
  },
  umlautadaptarr: {
    base_url: null,
    enabled: false,
    username: null,
  },
};

function configuredSecret(label: string): HTMLInputElement {
  const input = screen.getByLabelText(label);
  expect(input).toHaveAttribute("type", "password");
  expect(input).toHaveValue("");
  expect(input).toHaveAttribute(
    "placeholder",
    expect.stringMatching(/configured/i),
  );
  return input as HTMLInputElement;
}

describe("settings", () => {
  beforeEach(() => {
    visit("/settings");
  });

  it("renders the SettingsStore public view with top-level connectors and write-only secret flags", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
    });

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Settings" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    const crowdnfo = screen.getByRole("group", { name: "CrowdNFO" });
    expect(within(crowdnfo).getByLabelText("Base URL")).toHaveValue(
      "https://crowdnfo.net/",
    );
    configuredSecret("CrowdNFO API key");

    const qbittorrent = screen.getByRole("group", { name: "qBittorrent" });
    expect(within(qbittorrent).getByLabelText("URL")).toHaveValue(
      "http://qbittorrent:8080/",
    );
    expect(within(qbittorrent).getByLabelText("Username")).toHaveValue(
      "crowdarrr",
    );
    configuredSecret("qBittorrent password");

    const sabnzbd = screen.getByRole("group", { name: "SABnzbd" });
    expect(within(sabnzbd).getByLabelText("URL")).toHaveValue(
      "http://sabnzbd:8080/",
    );
    configuredSecret("SABnzbd API key");

    const radarr = screen.getByRole("group", { name: "Radarr" });
    expect(within(radarr).getByLabelText("URL")).toHaveValue(
      "http://radarr:7878/",
    );
    configuredSecret("Radarr API key");

    const sonarr = screen.getByRole("group", { name: "Sonarr" });
    expect(within(sonarr).getByLabelText("URL")).toHaveValue(
      "http://sonarr:8989/",
    );
    configuredSecret("Sonarr API key");

    const umlaut = screen.getByRole("group", { name: "UmlautAdaptarr" });
    expect(within(umlaut).getByLabelText("URL")).toHaveValue("");

    expect(
      screen.getAllByRole("button", { name: /test .* connection/i }),
    ).toHaveLength(6);
  });

  it("honestly tests persisted connector settings and announces the result", async () => {
    let testRequest: MockRequest | undefined;
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
      "POST /api/connectors/qbittorrent/test": (request) => {
        testRequest = request;
        return jsonResponse({
          latency_ms: 18,
          message: "Connected to qBittorrent",
          status: "healthy",
        });
      },
    });
    const user = userEvent.setup();

    render(<App />);

    const qbittorrent = await screen.findByRole("group", {
      name: "qBittorrent",
    });
    await user.click(
      within(qbittorrent).getByRole("button", {
        name: /test qBittorrent connection/i,
      }),
    );

    expect(testRequest?.body).toBeUndefined();
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/connected to qBittorrent/i);
    expect(status).toHaveTextContent(/18 ms/i);
    expect(
      screen.getByText(/connection tests use .*saved settings/i),
    ).toHaveTextContent(/save changes before testing/i);
  });

  it("requires saving changed connection fields before testing", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
    });
    const user = userEvent.setup();

    render(<App />);

    const qbittorrent = await screen.findByRole("group", {
      name: "qBittorrent",
    });
    const url = within(qbittorrent).getByLabelText("URL");
    await user.clear(url);
    await user.type(url, "http://unsaved-qbit:8080");

    const testButton = within(qbittorrent).getByRole("button", {
      name: /test qBittorrent connection/i,
    });
    expect(testButton).toBeDisabled();
    expect(testButton).toHaveTextContent(/save first/i);
  });

  it("does not claim CrowdNFO is connected without a persisted API key", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse({
        ...settingsPublicView,
        secrets_configured: {
          ...settingsPublicView.secrets_configured,
          crowdnfo_api_key: false,
        },
      }),
    });
    const user = userEvent.setup();

    render(<App />);

    const testButton = await screen.findByRole("button", {
      name: /test CrowdNFO connection/i,
    });
    expect(testButton).toBeDisabled();
    expect(testButton).toHaveTextContent(/API key required/i);

    await user.type(screen.getByLabelText("CrowdNFO API key"), "unsaved-key");
    expect(testButton).toBeDisabled();
    expect(testButton).toHaveTextContent(/save first/i);
  });

  it("offers only valid CrowdNFO categories instead of asking for a save path", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
    });
    const user = userEvent.setup();

    render(<App />);

    const category = await screen.findByRole("combobox", {
      name: "CrowdNFO category 1",
    });
    expect(category).toHaveValue("Movies");
    expect(
      within(category)
        .getAllByRole("option")
        .map((option) => (option as HTMLOptionElement).value),
    ).toEqual(crowdNfoCategories);
    expect(screen.queryByLabelText("Save path 1")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Add category mapping" }),
    );
    await user.type(screen.getByLabelText("Category 2"), "sonarr");
    const addedCategory = screen.getByRole("combobox", {
      name: "CrowdNFO category 2",
    }) as HTMLSelectElement;
    expect(crowdNfoCategories).toContain(addedCategory.value);
    await user.selectOptions(addedCategory, "TV");
    expect(addedCategory).toHaveValue("TV");
  });

  it("submits only canonical SettingsPatch fields and normalises empty optional URLs", async () => {
    let savedSettings: Record<string, unknown> | undefined;
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
      "PUT /api/settings": (request: MockRequest) => {
        if (typeof request.body !== "string") {
          throw new Error("Expected settings to be submitted as JSON");
        }
        savedSettings = JSON.parse(request.body) as Record<string, unknown>;
        return jsonResponse(settingsPublicView);
      },
    });
    const user = userEvent.setup();

    render(<App />);

    const downloadMode = await screen.findByLabelText("Download mode");
    await user.selectOptions(downloadMode, "off");
    await user.click(screen.getByLabelText("Recheck after placing NFO"));
    await user.click(screen.getByLabelText("Contribute to CrowdNFO"));
    await user.click(screen.getByLabelText("Upload NFO"));
    await user.click(screen.getByLabelText("Upload MediaInfo"));
    await user.click(screen.getByLabelText("Upload file list"));
    await user.click(screen.getByLabelText("Dry run"));
    await user.click(screen.getByLabelText("Remove mismatched NFO"));
    await user.selectOptions(
      screen.getByLabelText("Match strategy"),
      "release_name_only",
    );
    const hashSize = screen.getByLabelText("Maximum hash size (GiB)");
    await user.clear(hashSize);
    await user.type(hashSize, "42");
    const schedule = screen.getByLabelText("Backfill schedule (cron)");
    await user.clear(schedule);
    await user.type(schedule, "30 2 * * 1");

    const qbittorrent = screen.getByRole("group", { name: "qBittorrent" });
    await user.click(within(qbittorrent).getByLabelText("Enable qBittorrent"));
    await user.clear(within(qbittorrent).getByLabelText("Username"));
    const sabnzbd = screen.getByRole("group", { name: "SABnzbd" });
    await user.clear(within(sabnzbd).getByLabelText("URL"));
    await user.type(screen.getByLabelText("CrowdNFO API key"), "new-crowd-key");
    await user.type(screen.getByLabelText("Radarr API key"), "new-radarr-key");

    await user.click(screen.getByRole("button", { name: "Add path mapping" }));
    await user.type(screen.getByLabelText("Connector path 2"), "/downloads");
    await user.type(screen.getByLabelText("Crowdarrr path 2"), "/data");
    await user.click(
      screen.getByRole("button", { name: "Remove path mapping 1" }),
    );

    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(Object.keys(savedSettings ?? {}).sort()).toEqual(
      [
        "auto_recheck",
        "backfill_cron",
        "category_mappings",
        "contribute",
        "crowdnfo",
        "download_mode",
        "dry_run",
        "hash_max_size_bytes",
        "match_strategy",
        "nfo_mismatch_policy",
        "path_mappings",
        "qbittorrent",
        "radarr",
        "sabnzbd",
        "sonarr",
        "umlautadaptarr",
      ].sort(),
    );
    expect(savedSettings).toMatchObject({
      auto_recheck: false,
      backfill_cron: "30 2 * * 1",
      category_mappings: { radarr: "Movies" },
      contribute: {
        enabled: false,
        filelist: true,
        mediainfo: false,
        nfo: false,
      },
      crowdnfo: {
        api_key: "new-crowd-key",
        base_url: "https://crowdnfo.net/",
      },
      download_mode: "off",
      dry_run: false,
      hash_max_size_bytes: 42 * 1024 ** 3,
      match_strategy: "release_name_only",
      nfo_mismatch_policy: "remove",
      path_mappings: [{ local_root: "/data", remote_root: "/downloads" }],
      qbittorrent: {
        base_url: "http://qbittorrent:8080/",
        enabled: false,
      },
      radarr: { api_key: "new-radarr-key" },
    });
    expect(savedSettings).not.toHaveProperty("mismatch_policy");
    for (const connectorName of ["sabnzbd", "umlautadaptarr"]) {
      const connector = savedSettings?.[connectorName] as
        Record<string, unknown> | undefined;
      expect(connector?.base_url ?? undefined).toBeUndefined();
    }
    expect(await screen.findByRole("status")).toHaveTextContent(
      /settings saved/i,
    );
  });

  it("shows loading progress and then renders settings", async () => {
    let resolveSettings: ((response: Response) => void) | undefined;
    const pendingSettings = new Promise<Response>((resolve) => {
      resolveSettings = resolve;
    });
    installFetchMock({
      "GET /api/settings": () => pendingSettings,
    });

    render(<App />);

    expect(screen.getByText("Loading settings…")).toBeVisible();
    resolveSettings?.(jsonResponse(settingsPublicView));
    expect(await screen.findByLabelText("Download mode")).toBeVisible();
    expect(screen.queryByText("Loading settings…")).not.toBeInTheDocument();
  });

  it("recovers from a settings load error through the retry action", async () => {
    let attempts = 0;
    installFetchMock({
      "GET /api/settings": () => {
        attempts += 1;
        return attempts === 1
          ? jsonResponse(
              { detail: "Settings database is busy" },
              { status: 503 },
            )
          : jsonResponse(settingsPublicView);
      },
    });
    const user = userEvent.setup();

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not load settings: settings database is busy/i,
    );
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByLabelText("Download mode")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it("keeps the form available when saving is rejected", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
      "PUT /api/settings": jsonResponse(
        { detail: "Backfill schedule is invalid" },
        { status: 422 },
      ),
    });
    const user = userEvent.setup();

    render(<App />);
    await user.click(
      await screen.findByRole("button", { name: "Save settings" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /backfill schedule is invalid/i,
    );
    expect(screen.getByLabelText("Download mode")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
  });

  it("attributes connector test failures to the selected service", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
      "POST /api/connectors/crowdnfo/test": jsonResponse(
        { detail: "API key rejected" },
        { status: 401 },
      ),
    });
    const user = userEvent.setup();

    render(<App />);
    await user.click(
      await screen.findByRole("button", {
        name: /test CrowdNFO connection/i,
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /CrowdNFO: API key rejected/i,
    );
  });

  it("renders an unhealthy HTTP-200 connector result as an error", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsPublicView),
      "POST /api/connectors/crowdnfo/test": jsonResponse({
        latency_ms: 12,
        message: "authentication failed",
        status: "unhealthy",
      }),
    });
    const user = userEvent.setup();

    render(<App />);
    await user.click(
      await screen.findByRole("button", {
        name: /test CrowdNFO connection/i,
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /CrowdNFO: authentication failed/i,
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
