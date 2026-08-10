import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { CandlePanel } from "@/components/CandlePanel";

describe("CandlePanel", () => {
  it("exposes per-field labels for the narrow-screen candle card presentation", () => {
    render(<CandlePanel candles={{ interval: "5m", candles: [{ instrument_key: "NSE:NIFTY50", interval: "5m", source_opened_at: "2026-08-03T04:25:00Z", source_closed_at: "2026-08-03T04:30:00Z", open: 24800, high: 24830, low: 24790, close: 24812.35, volume: 1200, finalized: true, source: "groww" }], served_at: "2026-08-03T04:30:01Z", correlation_id: "candle-correlation" }} />);

    expect(screen.getByLabelText("Close for candle 2026-08-03T04:30:00Z")).toHaveTextContent("24,812.35");
  });
});
