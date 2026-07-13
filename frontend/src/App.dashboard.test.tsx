import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { installFetchMock, jsonResponse, visit } from "./test/mockFetch";

const dashboardResponse = {
  connectors: [
    {
      id: "crowdnfo",
      latency_ms: 42,
      message: "Connected",
      name: "CrowdNFO",
      status: "healthy",
    },
    {
      id: "qbittorrent",
      latency_ms: null,
      message: "Authentication required",
      name: "qBittorrent",
      status: "unhealthy",
    },
    {
      id: "sabnzbd",
      latency_ms: null,
      message: "Not configured",
      name: "SABnzbd",
      status: "disabled",
    },
    {
      id: "radarr",
      latency_ms: 21,
      message: "Connected",
      name: "Radarr",
      status: "healthy",
    },
    {
      id: "sonarr",
      latency_ms: 24,
      message: "Connected",
      name: "Sonarr",
      status: "healthy",
    },
  ],
  counters: {
    fetched: 37,
    matches: 51,
    misses: 4,
    repaired: 9,
    uploaded: 14,
  },
  recent_activity: [
    {
      created_at: "2026-07-13T08:14:00Z",
      id: "activity-1",
      message: "Byte-exact NFO placed; recheck reached 100%",
      status: "success",
      title: "Example.Movie.2026.1080p-GROUP",
      type: "repair",
    },
    {
      created_at: "2026-07-13T08:10:00Z",
      id: "activity-2",
      message: "No CrowdNFO match yet; retry scheduled",
      miss_id: "miss-7",
      status: "warning",
      title: "Example.Show.S01E01-GROUP",
      type: "miss",
    },
  ],
  stuck_torrents: [
    {
      category: "cross-seed-link",
      hash: "0123456789abcdef",
      missing_nfo_path: "Sample.Release-GROUP/release.nfo",
      name: "Sample.Release-GROUP",
      progress: 0.999,
    },
  ],
};

describe("dashboard", () => {
  beforeEach(() => {
    visit("/");
  });

  it("renders a dark, navigable operational overview", async () => {
    installFetchMock({
      "GET /api/dashboard": jsonResponse(dashboardResponse),
    });

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Dashboard" }),
    ).toBeInTheDocument();
    expect(document.documentElement).toHaveClass("dark");

    const navigation = screen.getByRole("navigation", { name: /primary/i });
    expect(
      within(navigation).getByRole("link", { name: "Dashboard" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(navigation).getByRole("link", { name: "Settings" }),
    ).toHaveAttribute("href", "/settings");
    expect(
      within(navigation).getByRole("link", { name: "Live logs" }),
    ).toHaveAttribute("href", "/logs");

    const health = screen.getByRole("region", { name: /connector health/i });
    for (const connector of [
      "CrowdNFO",
      "qBittorrent",
      "SABnzbd",
      "Radarr",
      "Sonarr",
    ]) {
      expect(within(health).getByText(connector)).toBeInTheDocument();
    }
    expect(within(health).getAllByText("Connected")).toHaveLength(3);
    expect(within(health).getByText("Authentication required")).toBeVisible();
    expect(within(health).getByText("Not configured")).toBeVisible();

    const metrics = screen.getByRole("region", { name: /lifetime counters/i });
    for (const [label, value] of [
      ["Fetched", "37"],
      ["Torrents repaired", "9"],
      ["Uploaded", "14"],
      ["Matches", "51"],
      ["Misses", "4"],
    ]) {
      const metric = within(metrics).getByRole("group", { name: label });
      expect(within(metric).getByText(value)).toBeVisible();
    }

    const activity = screen.getByRole("feed", { name: /recent activity/i });
    expect(
      within(activity).getByText("Example.Movie.2026.1080p-GROUP"),
    ).toBeVisible();
    expect(within(activity).getByText(/recheck reached 100%/i)).toBeVisible();
    expect(within(activity).getByText(/retry scheduled/i)).toBeVisible();

    const stuck = screen.getByRole("region", { name: /stuck torrents/i });
    const torrentRow = within(stuck).getByRole("row", {
      name: /Sample\.Release-GROUP/i,
    });
    expect(within(torrentRow).getByText("99.9%")).toBeVisible();
    expect(within(torrentRow).getByText(/release\.nfo$/i)).toBeVisible();
    expect(
      within(torrentRow).getByRole("button", {
        name: /repair Sample\.Release-GROUP/i,
      }),
    ).toBeEnabled();
  });

  it("queues a scan and an individual torrent repair from semantic actions", async () => {
    let scanRequests = 0;
    let repairRequests = 0;
    let retryRequests = 0;
    installFetchMock({
      "GET /api/dashboard": jsonResponse(dashboardResponse),
      "POST /api/actions/scan-repair": () => {
        scanRequests += 1;
        return jsonResponse(
          { job_id: "scan-42", message: "Scan and repair queued" },
          { status: 202 },
        );
      },
      "POST /api/torrents/0123456789abcdef/repair": () => {
        repairRequests += 1;
        return jsonResponse(
          { job_id: "repair-9", message: "Repair queued" },
          { status: 202 },
        );
      },
      "POST /api/actions/misses/miss-7/retry": () => {
        retryRequests += 1;
        return jsonResponse(
          { job_id: "retry-3", message: "Match retry queued" },
          { status: 202 },
        );
      },
    });
    const user = userEvent.setup();

    render(<App />);

    await user.click(
      await screen.findByRole("button", { name: /scan & repair now/i }),
    );
    expect(scanRequests).toBe(1);
    expect(await screen.findByRole("status")).toHaveTextContent(
      /scan and repair queued/i,
    );

    await user.click(
      screen.getByRole("button", { name: /repair Sample\.Release-GROUP/i }),
    );
    expect(repairRequests).toBe(1);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(/repair queued/i);
    });

    await user.click(
      screen.getByRole("button", {
        name: /retry Example\.Show\.S01E01-GROUP/i,
      }),
    );
    expect(retryRequests).toBe(1);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        /match retry queued/i,
      );
    });
  });

  it("keeps all primary destinations keyboard-accessible in the compact shell", async () => {
    vi.mocked(window.matchMedia).mockImplementation(
      (query: string) =>
        ({
          matches: query.includes("max-width"),
          media: query,
          onchange: null,
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          addListener: vi.fn(),
          removeListener: vi.fn(),
          dispatchEvent: vi.fn(),
        }) as MediaQueryList,
    );
    installFetchMock({
      "GET /api/dashboard": jsonResponse(dashboardResponse),
    });
    const user = userEvent.setup();

    render(<App />);

    const menuButton = await screen.findByRole("button", {
      name: "Open navigation",
    });
    expect(menuButton).toHaveAttribute("aria-expanded", "false");

    await user.click(menuButton);

    expect(menuButton).toHaveAttribute("aria-expanded", "true");
    const compactNavigation = screen.getByRole("navigation", {
      name: /mobile navigation/i,
    });
    expect(
      within(compactNavigation).getByRole("link", { name: "Dashboard" }),
    ).toBeVisible();
    expect(
      within(compactNavigation).getByRole("link", { name: "Settings" }),
    ).toBeVisible();
    expect(
      within(compactNavigation).getByRole("link", { name: "Live logs" }),
    ).toBeVisible();

    await user.keyboard("{Escape}");
    expect(menuButton).toHaveAttribute("aria-expanded", "false");
    expect(menuButton).toHaveFocus();
  });

  it("shows an actionable retry state without taking down the app shell", async () => {
    let attempts = 0;
    installFetchMock({
      "GET /api/dashboard": () => {
        attempts += 1;
        return attempts === 1
          ? jsonResponse({ detail: "qBittorrent timed out" }, { status: 503 })
          : jsonResponse(dashboardResponse);
      },
    });
    const user = userEvent.setup();

    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not load dashboard/i);
    expect(alert).toHaveTextContent(/qBittorrent timed out/i);
    expect(
      screen.getByRole("navigation", { name: /primary/i }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /retry/i }));

    expect(
      await screen.findByRole("region", { name: /connector health/i }),
    ).toBeInTheDocument();
    expect(attempts).toBe(2);
  });
});
