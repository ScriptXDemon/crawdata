import type { MetricItem } from "../api/types";

// Layer 2 emits colors as CSS-var strings; "var(--ink)" maps to a muted token in this theme.
function color(c: string): string {
  return c === "var(--ink)" ? "var(--d-txt-3)" : c;
}

export function MetricStrip({
  metrics,
  active,
  onPick,
}: {
  metrics: MetricItem[];
  active: string;
  onPick: (filter: string) => void;
}) {
  if (metrics.length === 0) return null;
  return (
    <div className="metrics">
      {metrics.map((m) => (
        <div
          key={m.label}
          className={"metric" + (active === m.filter ? " on" : "")}
          onClick={() => onPick(m.filter)}
        >
          <span className="ml">
            <span className="d" style={{ background: color(m.color) }} />
            {m.label}
          </span>
          <span className="mv">{m.value}</span>
        </div>
      ))}
    </div>
  );
}
