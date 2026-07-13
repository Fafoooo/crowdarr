import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import App from "./App";
import { installFetchMock, jsonResponse, visit } from "./test/mockFetch";

const dashboardPublicView = {
  connectors: [],
  counters: {
    fetched: 0,
    matches: 0,
    misses: 0,
    repaired: 0,
    uploaded: 0,
  },
  recent_activity: [],
  stuck_torrents: [],
};

const settingsPublicView = {
  crowdnfo: { base_url: "https://crowdnfo.net/" },
  secrets_configured: {},
};

async function navigateToSettings() {
  const user = userEvent.setup();
  installFetchMock({
    "GET /api/dashboard": jsonResponse(dashboardPublicView),
    "GET /api/settings": jsonResponse(settingsPublicView),
  });
  render(<App />);
  await screen.findByRole("heading", { level: 1, name: "Dashboard" });
  await user.click(screen.getByRole("link", { name: "Settings" }));
  return screen.findByRole("heading", { level: 1, name: "Settings" });
}

describe("client-side route accessibility", () => {
  beforeEach(() => {
    document.title = "crowdarr";
    visit("/");
  });

  it("keeps the document title in sync with the active route", async () => {
    const heading = await navigateToSettings();

    expect(heading).toBeVisible();
    expect(document.title).toBe("Settings · crowdarr");
  });

  it("moves keyboard focus to the new page heading", async () => {
    const heading = await navigateToSettings();

    expect(heading).toHaveFocus();
  });

  it("announces route changes without moving visual content", async () => {
    await navigateToSettings();

    const announcement = screen.getByRole("status", {
      name: /current page/i,
    });
    expect(announcement).toHaveAttribute("aria-live", "polite");
    expect(announcement).toHaveAttribute("aria-atomic", "true");
    expect(announcement).toHaveTextContent("Settings");
  });
});
