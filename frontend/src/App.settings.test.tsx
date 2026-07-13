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

const settingsResponse = {
  backfill_cron: "0 3 * * *",
  category_mappings: [
    { category: "radarr", save_path: "/data/downloads/radarr" },
  ],
  connectors: {
    qbittorrent: {
      enabled: true,
      password: "server-must-not-expose-qbit-password",
      password_configured: true,
      url: "http://qbittorrent:8080",
      username: "crowdarrr",
    },
    radarr: {
      api_key: "server-must-not-expose-radarr-key",
      api_key_configured: true,
      enabled: true,
      url: "http://radarr:7878",
    },
    sabnzbd: {
      api_key: "server-must-not-expose-sab-key",
      api_key_configured: true,
      enabled: false,
      url: "http://sabnzbd:8080",
    },
    sonarr: {
      api_key: "server-must-not-expose-sonarr-key",
      api_key_configured: true,
      enabled: true,
      url: "http://sonarr:8989",
    },
    umlautadaptarr: {
      enabled: false,
      url: "http://umlautadaptarr:5005",
    },
  },
  contribution: {
    enabled: true,
    filelist: false,
    mediainfo: true,
    nfo: true,
  },
  crowdnfo: {
    api_key: "server-must-not-expose-crowdnfo-key",
    api_key_configured: true,
    base_url: "https://crowdnfo.net",
  },
  download_mode: "new_and_backfill",
  dry_run: true,
  matching: {
    max_hash_size_gib: 80,
    strategy: "hash_then_release_name",
  },
  mismatch_policy: "keep",
  path_mappings: [{ connector_path: "/data", local_path: "/data" }],
  recheck_after_repair: true,
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

  it("exposes every optional connector with labelled fields and write-only secrets", async () => {
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsResponse),
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
      "https://crowdnfo.net",
    );
    configuredSecret("CrowdNFO API key");

    const qbittorrent = screen.getByRole("group", { name: "qBittorrent" });
    expect(within(qbittorrent).getByLabelText("URL")).toHaveValue(
      "http://qbittorrent:8080",
    );
    expect(within(qbittorrent).getByLabelText("Username")).toHaveValue(
      "crowdarrr",
    );
    configuredSecret("qBittorrent password");

    const sabnzbd = screen.getByRole("group", { name: "SABnzbd" });
    expect(within(sabnzbd).getByLabelText("URL")).toHaveValue(
      "http://sabnzbd:8080",
    );
    configuredSecret("SABnzbd API key");

    const radarr = screen.getByRole("group", { name: "Radarr" });
    expect(within(radarr).getByLabelText("URL")).toHaveValue(
      "http://radarr:7878",
    );
    configuredSecret("Radarr API key");

    const sonarr = screen.getByRole("group", { name: "Sonarr" });
    expect(within(sonarr).getByLabelText("URL")).toHaveValue(
      "http://sonarr:8989",
    );
    configuredSecret("Sonarr API key");

    const umlaut = screen.getByRole("group", { name: "UmlautAdaptarr" });
    expect(within(umlaut).getByLabelText("URL")).toHaveValue(
      "http://umlautadaptarr:5005",
    );

    for (const leakedSecret of [
      "server-must-not-expose-qbit-password",
      "server-must-not-expose-sab-key",
      "server-must-not-expose-radarr-key",
      "server-must-not-expose-sonarr-key",
      "server-must-not-expose-crowdnfo-key",
    ]) {
      expect(screen.queryByDisplayValue(leakedSecret)).not.toBeInTheDocument();
      expect(screen.queryByText(leakedSecret)).not.toBeInTheDocument();
    }

    expect(
      screen.getAllByRole("button", { name: /test .* connection/i }),
    ).toHaveLength(6);
  });

  it("tests a connector in place and announces the result", async () => {
    let testRequests = 0;
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsResponse),
      "POST /api/connectors/qbittorrent/test": () => {
        testRequests += 1;
        return jsonResponse({
          latency_ms: 18,
          message: "Connected to qBittorrent",
          status: "healthy",
        });
      },
    });
    const user = userEvent.setup();

    render(<App />);

    await user.click(
      await screen.findByRole("button", {
        name: /test qBittorrent connection/i,
      }),
    );

    expect(testRequests).toBe(1);
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/connected to qBittorrent/i);
    expect(status).toHaveTextContent(/18 ms/i);
  });

  it("persists modes, safety controls, schedules, and mappings as one form", async () => {
    let savedSettings: Record<string, unknown> | undefined;
    installFetchMock({
      "GET /api/settings": jsonResponse(settingsResponse),
      "PUT /api/settings": (request: MockRequest) => {
        if (typeof request.body !== "string") {
          throw new Error("Expected settings to be submitted as JSON");
        }
        savedSettings = JSON.parse(request.body) as Record<string, unknown>;
        return jsonResponse({ ...settingsResponse, ...savedSettings });
      },
    });
    const user = userEvent.setup();

    render(<App />);

    const downloadMode = await screen.findByLabelText("Download mode");
    expect(downloadMode).toHaveValue("new_and_backfill");
    expect(
      within(downloadMode).getByRole("option", { name: "Off" }),
    ).toBeInTheDocument();
    expect(
      within(downloadMode).getByRole("option", { name: "New downloads only" }),
    ).toBeInTheDocument();
    expect(
      within(downloadMode).getByRole("option", {
        name: "New downloads + backfill",
      }),
    ).toBeInTheDocument();

    expect(screen.getByLabelText("Recheck after placing NFO")).toBeChecked();
    expect(screen.getByLabelText("Contribute to CrowdNFO")).toBeChecked();
    expect(screen.getByLabelText("Upload NFO")).toBeChecked();
    expect(screen.getByLabelText("Upload MediaInfo")).toBeChecked();
    expect(screen.getByLabelText("Upload file list")).not.toBeChecked();
    expect(screen.getByLabelText("Match strategy")).toHaveValue(
      "hash_then_release_name",
    );
    expect(screen.getByLabelText("Maximum hash size (GiB)")).toHaveValue(80);
    expect(screen.getByLabelText("Backfill schedule (cron)")).toHaveValue(
      "0 3 * * *",
    );
    expect(screen.getByLabelText("Dry run")).toBeChecked();
    expect(screen.getByLabelText("Keep mismatched NFO")).toBeChecked();
    expect(screen.getByLabelText("Remove mismatched NFO")).not.toBeChecked();

    expect(screen.getByLabelText("Connector path 1")).toHaveValue("/data");
    expect(screen.getByLabelText("Crowdarrr path 1")).toHaveValue("/data");
    expect(screen.getByLabelText("Category 1")).toHaveValue("radarr");
    expect(screen.getByLabelText("Save path 1")).toHaveValue(
      "/data/downloads/radarr",
    );

    await user.selectOptions(downloadMode, "off");
    await user.click(screen.getByLabelText("Dry run"));
    const schedule = screen.getByLabelText("Backfill schedule (cron)");
    await user.clear(schedule);
    await user.type(schedule, "30 2 * * 1");

    await user.click(screen.getByRole("button", { name: "Add path mapping" }));
    await user.type(screen.getByLabelText("Connector path 2"), "/downloads");
    await user.type(screen.getByLabelText("Crowdarrr path 2"), "/data");

    await user.click(
      screen.getByRole("button", { name: "Add category mapping" }),
    );
    await user.type(screen.getByLabelText("Category 2"), "sonarr");
    await user.type(
      screen.getByLabelText("Save path 2"),
      "/data/downloads/sonarr",
    );

    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(savedSettings).toMatchObject({
      backfill_cron: "30 2 * * 1",
      category_mappings: [
        { category: "radarr", save_path: "/data/downloads/radarr" },
        { category: "sonarr", save_path: "/data/downloads/sonarr" },
      ],
      download_mode: "off",
      dry_run: false,
      path_mappings: [
        { connector_path: "/data", local_path: "/data" },
        { connector_path: "/downloads", local_path: "/data" },
      ],
    });
    expect(await screen.findByRole("status")).toHaveTextContent(
      /settings saved/i,
    );
  });
});
