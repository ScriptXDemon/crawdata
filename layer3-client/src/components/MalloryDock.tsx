import { useState } from "react";

import { api } from "../api/client";
import { useScope } from "../scope";

interface Msg {
  role: "user" | "mallory";
  text: string;
  sources?: string[];
}

export function MalloryDock() {
  const { scope } = useScope();
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  async function send(text: string) {
    if (!text.trim() || busy) return;
    setMsgs((m) => [...m, { role: "user", text }]);
    setInput("");
    setBusy(true);
    try {
      const r = await api.mallory(text, scope.panel, scope.entityId);
      setMsgs((m) => [...m, { role: "mallory", text: r.answer, sources: r.sources }]);
    } catch {
      setMsgs((m) => [...m, { role: "mallory", text: "(Mallory is unavailable right now.)" }]);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button className="mallory-fab" onClick={() => setOpen(true)}>
        <span className="live" /> Ask Mallory
      </button>
    );
  }

  return (
    <div className="mallory">
      <div className="mallory-h">
        <span>
          <span className="live" /> Mallory <span className="mallory-scope">· {scope.label}</span>
        </span>
        <button className="modal-x" onClick={() => setOpen(false)} aria-label="Close">
          ✕
        </button>
      </div>
      <div className="mallory-body">
        {msgs.length === 0 && (
          <div className="mallory-hint">
            Ask about the {scope.label.toLowerCase()} in view — every answer is scoped to KSSL's data.
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={"mmsg " + m.role}>
            {m.text}
            {m.sources && m.sources.length > 0 && (
              <div className="msrc">src: {m.sources.join(", ")}</div>
            )}
          </div>
        ))}
        {busy && <div className="mmsg mallory">…</div>}
      </div>
      <div className="mallory-in">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send(input)}
          placeholder="Ask Mallory…"
        />
        <button onClick={() => send(input)} disabled={busy}>
          Send
        </button>
      </div>
    </div>
  );
}
