import { PILLAR_VIEWS, useNav } from "../nav";

// Left rail = the active pillar's views (Overview + numbered), with live counts. Mirrors the demo.
export function Rail() {
  const { pillar, view, counts, setView } = useNav();
  const views = PILLAR_VIEWS[pillar];

  return (
    <div className="rail">
      <div className="rail-sec svc-sec">
        <div
          className={"svc" + (view === "overview" ? " active" : "")}
          onClick={() => setView("overview")}
        >
          <span className="ix">
            <i className="ti ti-layout-grid" aria-hidden="true" style={{ fontSize: 13 }} />
          </span>
          <span className="nm">Overview</span>
          <span className="tdot" />
          <span className="ct">{counts[pillar] ?? "—"}</span>
        </div>
      </div>
      <div className="rail-sec svc-sec" style={{ borderTop: "none" }}>
        {views.slice(1).map((v, i) => (
          <div
            key={v.view}
            className={"svc" + (view === v.view ? " active" : "")}
            onClick={() => setView(v.view)}
          >
            <span className="ix">{String(i + 1).padStart(2, "0")}</span>
            <span className="nm">{v.label}</span>
            <span className="tdot none" />
            <span className="ct">{counts[v.countKey] ?? "—"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
