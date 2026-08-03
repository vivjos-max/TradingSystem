import { formatInteger, formatNumber, formatTime, unavailable } from "@/lib/format";
import type { NiftyOptionChainResponse, OptionContractResponse } from "@/lib/types";

const columns: Array<[string, keyof OptionContractResponse]> = [
  ["LTP", "ltp"], ["Bid", "bid"], ["Ask", "ask"], ["Spread", "spread"], ["Volume", "volume"], ["OI", "open_interest"], ["IV", "implied_volatility"], ["Δ", "delta"], ["Γ", "gamma"], ["Θ", "theta"], ["Vega", "vega"], ["Rho", "rho"]
];

function displayValue(key: keyof OptionContractResponse, value: OptionContractResponse[keyof OptionContractResponse]): string {
  return key === "volume" || key === "open_interest" ? formatInteger(value as number | null) : formatNumber(value as number | null);
}

export function OptionTable({ optionChain }: { optionChain: NiftyOptionChainResponse | null }) {
  if (!optionChain) return <section className="panel"><h2>Option chain</h2><p>No option chain is available.</p></section>;
  return (
    <section className="panel table-panel" aria-labelledby="options-heading">
      <div className="panel-heading"><div><h2 id="options-heading">Option chain</h2><p>Expiry {optionChain.expiry} · ATM {formatInteger(optionChain.atm_strike)}</p></div><time dateTime={optionChain.source_observed_at}>{formatTime(optionChain.source_observed_at)}</time></div>
      <div className="table-scroll">
        <table>
          <thead><tr><th>Strike</th>{columns.flatMap(([label]) => [<th key={`CE-${label}`}>CE {label}</th>, <th key={`PE-${label}`}>PE {label}</th>])}</tr></thead>
          <tbody>{optionChain.selected_strikes.map((strike) => {
            const ce = optionChain.contracts.find((contract) => contract.strike === strike && contract.option_type === "CE");
            const pe = optionChain.contracts.find((contract) => contract.strike === strike && contract.option_type === "PE");
            const formattedStrike = formatInteger(strike);
            return <tr key={strike}><td data-label="Strike" aria-label={`Strike ${formattedStrike}`}>{formattedStrike}</td>{columns.flatMap(([label, key]) => ([
              <td key={`CE-${label}`} data-label={`CE ${label}`} aria-label={`CE ${label} for ${formattedStrike}`}>{ce ? displayValue(key, ce[key]) : unavailable}</td>,
              <td key={`PE-${label}`} data-label={`PE ${label}`} aria-label={`PE ${label} for ${formattedStrike}`}>{pe ? displayValue(key, pe[key]) : unavailable}</td>
            ]))}</tr>;
          })}</tbody>
        </table>
      </div>
    </section>
  );
}
