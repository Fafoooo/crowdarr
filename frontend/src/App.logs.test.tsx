import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import App from "./App";
import { installFetchMock, jsonResponse, visit } from "./test/mockFetch";

const initialLogs = {
  items: [
    {
      context: { hash: "0123456789abcdef", strategy: "media_sha256" },
      event: "repair.completed",
      id: "log-2",
      level: "info",
      message: "Torrent reached 100% after recheck",
      timestamp: "2026-07-13T08:14:02Z",
    },
    {
      context: { connector: "qbittorrent" },
      event: "connector.timeout",
      id: "log-1",
      level: "warning",
      message: "qBittorrent health check timed out; scan continued",
      timestamp: "2026-07-13T08:13:00Z",
    },
  ],
  next_cursor: "log-2",
};

describe("live logs", () => {
  beforeEach(() => {
    visit("/logs");
  });

  it("offers an accessible live region, filtering, pause, and manual refresh", async () => {
    let requests = 0;
    installFetchMock({
      "GET /api/logs": () => {
        requests += 1;
        return jsonResponse(
          requests === 1
            ? initialLogs
            : {
                items: [
                  {
                    context: { release: "Example.Movie.2026-GROUP" },
                    event: "crowdnfo.upload_failed",
                    id: "log-3",
                    level: "error",
                    message: "CrowdNFO rejected the upload",
                    timestamp: "2026-07-13T08:15:00Z",
                  },
                  ...initialLogs.items,
                ],
                next_cursor: "log-3",
              },
        );
      },
    });
    const user = userEvent.setup();

    render(<App />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Live logs" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Live logs" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    const stream = screen.getByRole("log", { name: /live log stream/i });
    expect(stream).toHaveAttribute("aria-live", "polite");
    expect(
      within(stream).getByText("Torrent reached 100% after recheck"),
    ).toBeVisible();
    expect(
      within(stream).getByText(/health check timed out; scan continued/i),
    ).toBeVisible();
    expect(within(stream).getByText("repair.completed")).toBeVisible();
    expect(within(stream).getByText("INFO")).toBeVisible();
    expect(within(stream).getByText("WARNING")).toBeVisible();

    await user.click(
      screen.getByRole("button", { name: "Pause live updates" }),
    );
    expect(
      screen.getByRole("button", { name: "Resume live updates" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Refresh logs" }));
    expect(requests).toBe(2);
    expect(
      await within(stream).findByText("CrowdNFO rejected the upload"),
    ).toBeVisible();

    await user.selectOptions(screen.getByLabelText("Log level"), "error");
    expect(
      within(stream).getByText("CrowdNFO rejected the upload"),
    ).toBeVisible();
    expect(
      within(stream).queryByText("Torrent reached 100% after recheck"),
    ).not.toBeInTheDocument();
  });

  it("keeps existing entries visible when a refresh fails", async () => {
    let requests = 0;
    installFetchMock({
      "GET /api/logs": () => {
        requests += 1;
        return requests === 1
          ? jsonResponse(initialLogs)
          : jsonResponse(
              { detail: "Log storage is temporarily unavailable" },
              { status: 503 },
            );
      },
    });
    const user = userEvent.setup();

    render(<App />);

    expect(
      await screen.findByText("Torrent reached 100% after recheck"),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Refresh logs" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /log storage is temporarily unavailable/i,
    );
    expect(
      screen.getByText("Torrent reached 100% after recheck"),
    ).toBeVisible();
    expect(requests).toBe(2);
  });
});
