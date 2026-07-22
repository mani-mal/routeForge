// Top control bar — Headroom on/off, the four scripted personas, and an
// honest note about the controls that are deferred for this prototype.
import { SCRIPTED } from "../api";
import type { Controls } from "../api";

export function ControlPanel({
  controls,
  onToggleHeadroom,
  togglingHeadroom,
  onFireScripted,
  firingIndex,
  onRunTraffic,
  trafficRunning,
}: {
  controls: Controls | null;
  onToggleHeadroom: () => void;
  togglingHeadroom: boolean;
  onFireScripted: (index: number) => void;
  firingIndex: number | null;
  onRunTraffic: () => void;
  trafficRunning: boolean;
}) {
  const headroomOn = controls?.headroom_enabled ?? false;

  return (
    <div className="border-b border-edge bg-panel/60 px-4 py-3 flex flex-wrap items-center gap-4">
      <button
        onClick={onToggleHeadroom}
        disabled={!controls || togglingHeadroom}
        className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-50 ${
          headroomOn ? "border-cheap bg-cheap/10 text-cheap" : "border-edge bg-ink/40 text-gray-400"
        }`}
      >
        <span className={`h-2.5 w-2.5 rounded-full ${headroomOn ? "bg-cheap" : "bg-gray-600"}`} />
        Headroom {headroomOn ? "on" : "off"}
      </button>

      <div className="flex flex-wrap items-center gap-2">
        {SCRIPTED.map((s, i) => (
          <button
            key={s.label}
            onClick={() => onFireScripted(i)}
            disabled={firingIndex !== null}
            title={s.hint}
            className="flex flex-col items-start rounded-md border border-edge bg-ink/40 px-3 py-1.5 text-left hover:border-gray-600 disabled:opacity-50"
          >
            <span className="text-sm font-medium text-gray-200">
              {firingIndex === i ? "firing…" : s.label}
            </span>
            <span className="text-[11px] text-gray-500">{s.hint}</span>
          </button>
        ))}
      </div>

      <button
        onClick={onRunTraffic}
        disabled={trafficRunning}
        title="Fire a 100-request seeded traffic burst at the live API so the dashboard fills"
        className="flex items-center gap-2 rounded-md border border-edge bg-ink/40 px-3 py-2 text-sm font-medium text-gray-200 hover:border-gray-600 disabled:opacity-50"
      >
        <span className={`h-2.5 w-2.5 rounded-full ${trafficRunning ? "bg-mid animate-pulse" : "bg-gray-600"}`} />
        {trafficRunning ? "running…" : "Run 100 burst"}
      </button>

      {controls && controls.deferred_controls.length > 0 && (
        <span className="ml-auto text-[11px] text-gray-600">
          coming later: {controls.deferred_controls.join(", ")}
        </span>
      )}
    </div>
  );
}
