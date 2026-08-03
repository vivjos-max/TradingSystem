import { formatTime } from "@/lib/format";

interface GlobalStatusProps {
  status: string;
  reason?: string;
  asOf: string | null;
}

export function GlobalStatus({ status, reason, asOf }: GlobalStatusProps) {
  const normalizedStatus = status.toUpperCase();
  return (
    <section className={`global-status status-${normalizedStatus.toLowerCase()}`} role="status" aria-live="polite">
      <div>
        <p className="eyebrow">Operational state</p>
        <strong>{normalizedStatus}</strong>
      </div>
      <div className="status-detail">
        {reason ? <p>{reason}</p> : <p>Read-only dashboard. Values are displayed as received.</p>}
        {asOf ? <time dateTime={asOf}>Source observed {formatTime(asOf)}</time> : <span>Awaiting source timestamp</span>}
      </div>
    </section>
  );
}
