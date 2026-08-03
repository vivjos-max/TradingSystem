import { formatNumber, formatTime } from "@/lib/format";
import type { NiftyQuoteResponse } from "@/lib/types";

export function MarketPanel({ quote }: { quote: NiftyQuoteResponse | null }) {
  if (!quote) return <section className="panel"><h2>Market</h2><p>No quote is available.</p></section>;
  return (
    <section className="panel" aria-labelledby="market-heading">
      <div className="panel-heading"><h2 id="market-heading">NIFTY market</h2><span>Read-only quote</span></div>
      <div className="metric-grid">
        <article><span>Spot</span><strong>{formatNumber(quote.spot.last_price)}</strong><time dateTime={quote.spot.source_observed_at}>{formatTime(quote.spot.source_observed_at)}</time></article>
        <article><span>Nearest future</span><strong>{formatNumber(quote.nearest_future.last_price)}</strong><time dateTime={quote.nearest_future.source_observed_at}>{formatTime(quote.nearest_future.source_observed_at)}</time></article>
      </div>
    </section>
  );
}
