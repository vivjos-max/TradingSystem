import { formatInteger, formatNumber, formatTime } from "@/lib/format";
import type { NiftyCandlesResponse } from "@/lib/types";

export function CandlePanel({ candles }: { candles: NiftyCandlesResponse | null }) {
  if (!candles) return <section className="panel"><h2>Candles</h2><p>No candles are available.</p></section>;
  return <section className="panel table-panel" aria-labelledby="candles-heading"><div className="panel-heading"><h2 id="candles-heading">NIFTY {candles.interval} candles</h2><span>{candles.candles.length} supplied</span></div><div className="table-scroll"><table><thead><tr><th>Closed at</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>Final</th></tr></thead><tbody>{candles.candles.slice(-12).reverse().map((candle) => <tr key={`${candle.instrument_key}-${candle.source_opened_at}`}><td><time dateTime={candle.source_closed_at}>{formatTime(candle.source_closed_at)}</time></td><td>{formatNumber(candle.open)}</td><td>{formatNumber(candle.high)}</td><td>{formatNumber(candle.low)}</td><td>{formatNumber(candle.close)}</td><td>{formatInteger(candle.volume)}</td><td>{candle.finalized ? "Yes" : "No"}</td></tr>)}</tbody></table></div></section>;
}
