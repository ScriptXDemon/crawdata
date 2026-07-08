import { type Pillar, useNav } from "../nav";
import { ReportButton } from "./ReportButton";

const PILLARS: [Pillar, string][] = [
  ["competitive", "Competitive"],
  ["market", "Market"],
  ["technology", "Technology"],
];

export function TopBar() {
  const { pillar, setPillar } = useNav();
  return (
    <div className="topbar">
      <div className="brand">
        <span className="wordmark">
          MALLORY <span className="prod">Intel</span>
        </span>
      </div>
      <div className="topnav">
        {PILLARS.map(([id, label]) => (
          <div
            key={id}
            className={"pill" + (pillar === id ? " active" : "")}
            onClick={() => setPillar(id)}
          >
            <span className="dot" />
            {label}
          </div>
        ))}
      </div>
      <div className="topright">
        <ReportButton />
        <div className="statusbox">
          <span className="k">Status</span>
          <span className="v">
            <span className="live" />
            Live
          </span>
        </div>
        <div className="statusbox client">
          <span className="k">Anchor client</span>
          <span className="v">KSSL</span>
        </div>
      </div>
    </div>
  );
}
