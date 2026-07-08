import type { SignalCard } from "../api/types";

export function AlertRow({
  signal,
  selected,
  onClick,
}: {
  signal: SignalCard;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <div className={"alert" + (selected ? " sel" : "")} onClick={onClick}>
      <div className="gut">
        <span className={`sig ${signal.dir}`} />
        <span className="rank">{String(signal.rank).padStart(2, "0")}</span>
      </div>
      <div className="body">
        <div className="ttl">{signal.title}</div>
        {signal.meta && <div className="meta">{signal.meta}</div>}
        {signal.sowhat && <div className="sowhat">{signal.sowhat}</div>}
      </div>
      <div className="aside">
        <span className={`dirtag ${signal.dir}`}>{signal.dir}</span>
        {signal.ago_display && <span className="ago">{signal.ago_display}</span>}
        {signal.provenance === "sourced" && <span className="srcbadge">src</span>}
      </div>
    </div>
  );
}
