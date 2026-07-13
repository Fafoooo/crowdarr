import { describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest } from "./client";
import {
  installFetchMock,
  jsonResponse,
  type MockRequest,
} from "../test/mockFetch";

describe("apiRequest", () => {
  it("uses same-origin credentials, JSON headers, and returns parsed data", async () => {
    let received: MockRequest | undefined;
    installFetchMock({
      "POST /api/example": (request) => {
        received = request;
        return jsonResponse({ accepted: true }, { status: 201 });
      },
    });

    const result = await apiRequest<{ accepted: boolean }>("/api/example", {
      body: JSON.stringify({ mode: "new_only" }),
      method: "POST",
    });

    expect(result).toEqual({ accepted: true });
    expect(received?.method).toBe("POST");
    expect(received?.credentials).toBe("same-origin");
    expect(received?.headers.get("accept")).toBe("application/json");
    expect(received?.headers.get("content-type")).toBe("application/json");
    expect(received?.body).toBe('{"mode":"new_only"}');
  });

  it("returns undefined for a successful empty response", async () => {
    installFetchMock({
      "DELETE /api/misses/miss-1": new Response(null, { status: 204 }),
    });

    await expect(
      apiRequest<void>("/api/misses/miss-1", { method: "DELETE" }),
    ).resolves.toBeUndefined();
  });

  it("preserves the backend detail and status in a typed API error", async () => {
    installFetchMock({
      "PUT /api/settings": jsonResponse(
        { detail: "Backfill schedule is not a valid cron expression" },
        { status: 422 },
      ),
    });

    const error = await apiRequest("/api/settings", {
      body: JSON.stringify({ backfill_cron: "not cron" }),
      method: "PUT",
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      detail: "Backfill schedule is not a valid cron expression",
      message: "Backfill schedule is not a valid cron expression",
      name: "ApiError",
      status: 422,
    });
  });

  it("creates a useful fallback error when the response is not JSON", async () => {
    installFetchMock({
      "GET /api/dashboard": new Response("proxy returned HTML", {
        headers: { "Content-Type": "text/html" },
        status: 502,
        statusText: "Bad Gateway",
      }),
    });

    const error = await apiRequest("/api/dashboard").catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      detail: "Request failed with status 502",
      message: "Request failed with status 502",
      status: 502,
    });
  });

  it("normalizes network failures while preserving the original cause", async () => {
    const cause = new TypeError("Failed to fetch");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(cause));

    const error = await apiRequest("/api/dashboard").catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      cause,
      detail: "Network request failed",
      message: "Network request failed",
      status: 0,
    });
  });

  it("does not turn intentional request cancellation into a user-facing error", async () => {
    const aborted = new DOMException("The operation was aborted", "AbortError");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(aborted));

    await expect(apiRequest("/api/logs")).rejects.toBe(aborted);
  });
});
