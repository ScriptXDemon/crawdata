import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import { api } from "./api/client";

// The demo's flow: 3 pillars on top; each pillar's VIEWS live in the left rail (numbered, with counts).
export type Pillar = "competitive" | "market" | "technology";

export interface ViewDef {
  view: string;
  label: string;
  countKey: string; // key into nav counts; "" for overview (resolved per pillar)
}

export const PILLAR_VIEWS: Record<Pillar, ViewDef[]> = {
  competitive: [
    { view: "overview", label: "Overview", countKey: "" },
    { view: "positioning", label: "Positioning", countKey: "matchups" },
    { view: "network", label: "Network", countKey: "" },
    { view: "partnerships", label: "Partnerships", countKey: "partnerships" },
    { view: "geo", label: "Geo Footprint", countKey: "geo" },
    { view: "patents-comp", label: "Patents", countKey: "patents" },
  ],
  market: [
    { view: "overview", label: "Overview", countKey: "" },
    { view: "tender", label: "Tender Pipeline", countKey: "tenders" },
  ],
  technology: [
    { view: "overview", label: "Overview", countKey: "" },
    { view: "innovation", label: "Innovation Pipeline", countKey: "innovation" },
    { view: "patents-tech", label: "Patents", countKey: "patents" },
  ],
};

interface NavCtx {
  pillar: Pillar;
  view: string;
  counts: Record<string, number>;
  setPillar: (p: Pillar) => void;
  setView: (v: string) => void;
}

const Ctx = createContext<NavCtx | null>(null);

export function NavProvider({ children }: { children: ReactNode }) {
  const [pillar, setPillarState] = useState<Pillar>("competitive");
  const [view, setView] = useState("overview");
  const [counts, setCounts] = useState<Record<string, number>>({});

  useEffect(() => {
    api.navCounts().then(setCounts).catch(() => undefined);
  }, []);

  // Switching pillar resets to that pillar's Overview (matches the demo).
  const setPillar = (p: Pillar) => {
    setPillarState(p);
    setView("overview");
  };

  return (
    <Ctx.Provider value={{ pillar, view, counts, setPillar, setView }}>{children}</Ctx.Provider>
  );
}

export function useNav(): NavCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useNav must be used within NavProvider");
  return c;
}
