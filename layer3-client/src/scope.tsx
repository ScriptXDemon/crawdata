import { createContext, useContext, useState, type ReactNode } from "react";

// Tracks what the user is looking at so Mallory can scope its RAG to the right serving rows.
export interface Scope {
  panel: string; // overview | signal | tender | matchup | competitor
  entityId: string | null;
  label: string; // human label shown in the Mallory dock
}

interface ScopeCtx {
  scope: Scope;
  setScope: (s: Scope) => void;
}

const Ctx = createContext<ScopeCtx | null>(null);

export function ScopeProvider({ children }: { children: ReactNode }) {
  const [scope, setScope] = useState<Scope>({ panel: "overview", entityId: null, label: "Overview" });
  return <Ctx.Provider value={{ scope, setScope }}>{children}</Ctx.Provider>;
}

export function useScope(): ScopeCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useScope must be used within ScopeProvider");
  return c;
}
