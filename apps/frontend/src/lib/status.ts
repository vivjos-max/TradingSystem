import { createElement } from "react";

export type StatusBannerProps = {
  state: string;
  sourceAt?: string | null;
  reason?: string | null;
};

export function StatusBanner({ state, sourceAt, reason }: StatusBannerProps) {
  return createElement(
    "section",
    {
      className: `status-banner status-${state.toLowerCase().replaceAll("_", "-")}`,
      role: "status",
      "aria-live": "polite",
      "aria-atomic": "true",
    },
    createElement("p", { className: "eyebrow" }, "System status"),
    createElement("h2", null, state),
    createElement("p", null, "Source time: ", sourceAt || "—"),
    reason ? createElement("p", { className: "status-reason" }, "Reason: ", reason) : null,
  );
}
