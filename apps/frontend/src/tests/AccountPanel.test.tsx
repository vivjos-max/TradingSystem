import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { AccountPanel } from "@/components/AccountPanel";

describe("AccountPanel", () => {
  it("shows the orders source timestamp next to today's record count", () => {
    render(
      <AccountPanel
        account={{ available_cash: 100000, used_margin: 25000, collateral: 5000, source: "groww", source_observed_at: "2026-08-03T04:30:00Z", positions: [], served_at: "2026-08-03T04:30:01Z", correlation_id: "account-correlation" }}
        orders={{ orders: [], unmatched_fills: [], orders_source: "groww", orders_source_observed_at: "2026-08-03T04:35:00Z", fills_source: "groww", fills_source_observed_at: "2026-08-03T04:35:00Z", served_at: "2026-08-03T04:35:01Z", correlation_id: "orders-correlation" }}
      />
    );

    expect(screen.getByText(/today/i)).toBeVisible();
    expect(screen.getByText(/10:05:00/)).toBeVisible();
  });
});
