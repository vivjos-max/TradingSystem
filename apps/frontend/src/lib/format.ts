export const unavailable = "—";

export function formatNumber(value: number | null, digits = 2): string {
  if (value === null) return unavailable;
  return new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  }).format(value);
}

export function formatInteger(value: number | null): string {
  if (value === null) return unavailable;
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(value);
}

export function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toISOString().replace("T", " ").replace(".000Z", "Z");
}
