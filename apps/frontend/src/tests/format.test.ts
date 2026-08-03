import { describe, expect, it } from "vitest";
import { formatTime } from "@/lib/format";

describe("formatTime", () => {
  it("renders valid source instants in the India exchange timezone", () => {
    expect(formatTime("2026-08-03T04:30:00Z")).toContain("10:00:00");
  });
});
