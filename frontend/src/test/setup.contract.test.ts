import { describe, expect, it } from "vitest";

describe("frontend test harness", () => {
  it("provides the browser primitives used by the responsive shell", () => {
    expect(window.matchMedia("(max-width: 768px)").matches).toBe(false);
    expect(document.body).toBeInTheDocument();
  });
});
