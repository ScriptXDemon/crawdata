import { MalloryDock } from "./components/MalloryDock";
import { TopBar } from "./components/TopBar";
import { NavProvider, useNav } from "./nav";
import { ScopeProvider } from "./scope";
import { GeoView } from "./views/GeoView";
import { InnovationView } from "./views/InnovationView";
import { NetworkView } from "./views/NetworkView";
import { OverviewView } from "./views/OverviewView";
import { PartnershipsView } from "./views/PartnershipsView";
import { PatentsView } from "./views/PatentsView";
import { PositioningView } from "./views/PositioningView";
import { TenderView } from "./views/TenderView";

function Body() {
  const { pillar, view } = useNav();
  switch (view) {
    case "overview":
      return <OverviewView key={pillar} pillar={pillar} />;
    case "positioning":
      return <PositioningView />;
    case "network":
      return <NetworkView />;
    case "partnerships":
      return <PartnershipsView />;
    case "geo":
      return <GeoView />;
    case "patents-comp":
      return <PatentsView mode="competitor" />;
    case "tender":
      return <TenderView />;
    case "innovation":
      return <InnovationView />;
    case "patents-tech":
      return <PatentsView mode="tech" />;
    default:
      return <OverviewView key={pillar} pillar={pillar} />;
  }
}

export function App() {
  return (
    <NavProvider>
      <ScopeProvider>
        <TopBar />
        <Body />
        <MalloryDock />
      </ScopeProvider>
    </NavProvider>
  );
}
