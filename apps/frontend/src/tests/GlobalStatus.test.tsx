import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { GlobalStatus } from "@/components/GlobalStatus";

describe("GlobalStatus", () => {
  it("renders blocked reason more prominently than cached prices", () => {
    render(
      <GlobalStatus
        status="BLOCKED"
        reason="Groww subscription unavailable"
        asOf="2026-08-03T04:30:00Z"
      />
    );

    expect(screen.getByRole("status")).toHaveTextContent("BLOCKED");
    expect(screen.getByText(/subscription unavailable/i)).toBeVisible();
    expect(screen.getByText(/10:00:00/i)).toBeVisible();
  });
});
