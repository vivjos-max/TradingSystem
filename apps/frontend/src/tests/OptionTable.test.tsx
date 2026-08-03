import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { OptionTable } from "@/components/OptionTable";
import type { NiftyOptionChainResponse } from "@/lib/types";

const optionChain: NiftyOptionChainResponse = {
  expiry: "2026-08-06",
  source: "groww",
  source_observed_at: "2026-08-03T04:30:00Z",
  atm_strike: 24800,
  selected_strikes: [24800],
  contracts: [
    {
      instrument_token: "NIFTY-CE-24800",
      strike: 24800,
      option_type: "CE",
      ltp: 131.25,
      bid: null,
      ask: null,
      spread: null,
      volume: null,
      open_interest: null,
      implied_volatility: null,
      delta: null,
      gamma: null,
      theta: null,
      vega: null,
      rho: null
    },
    {
      instrument_token: "NIFTY-PE-24800",
      strike: 24800,
      option_type: "PE",
      ltp: 109.5,
      bid: 109,
      ask: 110,
      spread: 1,
      volume: 2048,
      open_interest: 10240,
      implied_volatility: 12.4,
      delta: -0.42,
      gamma: 0.02,
      theta: -4.8,
      vega: 8.1,
      rho: -0.01
    }
  ],
  served_at: "2026-08-03T04:30:01Z",
  correlation_id: "test-correlation"
};

describe("OptionTable", () => {
  it("shows supplied CE and PE values while preserving unavailable fields", () => {
    render(<OptionTable optionChain={optionChain} />);

    expect(screen.getByRole("table")).toHaveTextContent("24,800");
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.getByText("131.25")).toBeVisible();
    expect(screen.getByText("-0.42")).toBeVisible();
    expect(screen.getByText(/10:00:00/i)).toBeVisible();
  });

  it("keeps each selected strike as one CE and PE row when a side is unavailable", () => {
    render(<OptionTable optionChain={{ ...optionChain, contracts: [optionChain.contracts[0]] }} />);

    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("columnheader", { name: "CE LTP" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "PE LTP" })).toBeVisible();
    expect(screen.getByLabelText("PE LTP for 24,800")).toHaveTextContent("—");
  });
});
