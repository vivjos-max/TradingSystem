import { formatTime } from "@/lib/format";
import type { ServiceResponse } from "@/lib/types";

export function ServiceGrid({ services }: { services: ServiceResponse[] }) {
  return (
    <section className="panel service-panel" aria-labelledby="services-heading">
      <div className="panel-heading"><h2 id="services-heading">Services</h2><span>Source timestamps</span></div>
      <div className="service-grid">
        {services.map((service) => (
          <article key={service.service} className={`service-card state-${service.state.toLowerCase()}`}>
            <strong>{service.service}</strong>
            <span>{service.state.toUpperCase()}</span>
            <time dateTime={service.source_observed_at}>{formatTime(service.source_observed_at)}</time>
          </article>
        ))}
      </div>
    </section>
  );
}
