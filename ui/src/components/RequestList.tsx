// Panel 1 (bottom half) — newest-first request list. Rows sharing a session_id
// (B17: client workflow-run correlation id) collapse under one expandable
// parent; ungrouped rows (no session_id) render flat as before.
import { useState } from "react";
import type { RequestSummary } from "../api";
import { Panel, TierDot, formatUsdPrecise, formatMs } from "./common";

type Group = { key: string; session_id: string | null; rows: RequestSummary[] };

// requests arrive newest-first; the first time a session id is seen is its
// newest member, so a group's position follows its newest row. Ungrouped rows
// (null session_id) become singleton null-session groups, rendered flat.
function groupBySession(requests: RequestSummary[]): Group[] {
  const groups: Group[] = [];
  const bySession = new Map<string, Group>();
  for (const r of requests) {
    if (!r.session_id) {
      groups.push({ key: r.id, session_id: null, rows: [r] });
      continue;
    }
    let g = bySession.get(r.session_id);
    if (!g) {
      g = { key: `session:${r.session_id}`, session_id: r.session_id, rows: [] };
      bySession.set(r.session_id, g);
      groups.push(g);
    }
    g.rows.push(r);
  }
  return groups;
}

function RequestRow({
  r,
  selected,
  onSelect,
  indent = false,
}: {
  r: RequestSummary;
  selected: boolean;
  onSelect: (id: string) => void;
  indent?: boolean;
}) {
  return (
    <button
      onClick={() => onSelect(r.id)}
      className={`text-left rounded-md border px-3 py-2 text-sm transition-colors ${indent ? "ml-3" : ""} ${
        selected ? "border-cheap bg-cheap/10" : "border-edge bg-ink/40 hover:border-gray-600"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <TierDot tier={r.tier} />
          <span className="font-medium truncate" title={r.model}>{r.model_display ?? r.model}</span>
          {r.onprem_only && (
            <span className="shrink-0 rounded bg-mid/20 text-mid text-[10px] font-semibold px-1.5 py-0.5">
              on-prem
            </span>
          )}
          {r.hops > 1 && (
            <span className="shrink-0 rounded bg-gray-700 text-gray-300 text-[10px] font-semibold px-1.5 py-0.5">
              {r.hops} hops
            </span>
          )}
        </div>
        <span className="shrink-0 text-xs font-mono text-gray-400">{formatUsdPrecise(r.total_usd)}</span>
      </div>
      <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
        <span>{r.sensitivity}</span>
        <span className="font-mono">{formatMs(r.latency_ms)}</span>
      </div>
    </button>
  );
}

function SessionGroup({
  group,
  collapsed,
  onToggle,
  selectedId,
  onSelect,
}: {
  group: Group;
  collapsed: boolean;
  onToggle: () => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const totalUsd = group.rows.reduce((s, r) => s + r.total_usd, 0);
  const totalMs = group.rows.reduce((s, r) => s + r.latency_ms, 0);
  const sensitivity = group.rows[0].sensitivity; // representative (one run = one policy)
  return (
    <div className="rounded-md border border-edge bg-ink/20">
      <button onClick={onToggle} className="w-full text-left px-3 py-2 hover:bg-ink/40 transition-colors">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <span className="shrink-0 text-gray-500 w-3">{collapsed ? "▸" : "▾"}</span>
            <span className="font-mono text-xs truncate text-gray-300" title={group.session_id ?? ""}>
              {group.session_id}
            </span>
            <span className="shrink-0 rounded bg-gray-700 text-gray-300 text-[10px] font-semibold px-1.5 py-0.5">
              {group.rows.length} calls
            </span>
          </div>
          <span className="shrink-0 text-xs font-mono text-gray-400">{formatUsdPrecise(totalUsd)}</span>
        </div>
        <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
          <span>{sensitivity}</span>
          <span className="font-mono">{formatMs(totalMs)}</span>
        </div>
      </button>
      {!collapsed && (
        <div className="flex flex-col gap-1.5 px-2 pb-2">
          {group.rows.map((r) => (
            <RequestRow key={r.id} r={r} selected={r.id === selectedId} onSelect={onSelect} indent />
          ))}
        </div>
      )}
    </div>
  );
}

export function RequestList({
  requests,
  selectedId,
  onSelect,
}: {
  requests: RequestSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const groups = groupBySession(requests);
  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <Panel title="Requests" right={<span className="text-xs text-gray-500">{requests.length}</span>} className="flex-1 min-h-0">
      {requests.length === 0 ? (
        <p className="text-sm text-gray-500">No requests yet — they appear here as connected applications call the API.</p>
      ) : (
        <div className="flex flex-col gap-1.5 overflow-y-auto max-h-[420px] pr-1">
          {groups.map((g) =>
            g.session_id === null ? (
              <RequestRow key={g.key} r={g.rows[0]} selected={g.rows[0].id === selectedId} onSelect={onSelect} />
            ) : (
              <SessionGroup
                key={g.key}
                group={g}
                collapsed={collapsed.has(g.key)}
                onToggle={() => toggle(g.key)}
                selectedId={selectedId}
                onSelect={onSelect}
              />
            ),
          )}
        </div>
      )}
    </Panel>
  );
}
