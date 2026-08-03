import { formatInteger, formatNumber, formatTime } from "@/lib/format";
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
          <thead><tr><th>Strike</th><th>Side</th>{columns.map(([label]) => <th key={label}>{label}</th>)}</tr></thead>
          <tbody>{optionChain.contracts.map((contract) => <tr key={contract.instrument_token}><td>{formatInteger(contract.strike)}</td><td>{contract.option_type}</td>{columns.map(([label, key]) => <td key={label}>{displayValue(key, contract[key])}</td>)}</tr>)}</tbody>
        </table>
      </div>
    </section>
  );
}
