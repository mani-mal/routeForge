import type { MetricsAlert } from "../../api";
import { Panel } from "../../components/common";

// Icon + text per alert; severity never rests on colour alone.
export function AlertsList({ alerts }: { alerts: MetricsAlert[] }) {
  if (alerts.length === 0) return null;
  return (
    <Panel title={`Needs attention (${alerts.length})`}>
      <ul className="flex flex-col gap-1.5">
        {alerts.map((a, i) => {
          const warn = a.severity === "warning";
          return (
            <li key={`${a.kind}-${a.model ?? ""}-${i}`} className="flex items-start gap-2 text-sm text-gray-200">
              <span
                className={`font-semibold ${warn ? "text-amber-400" : "text-sky-400"}`}
                aria-label={warn ? "Warning" : "Information"}
              >
                {warn ? "⚠" : "ⓘ"}
              </span>
              <span>
                <span className="font-semibold text-gray-100">{warn ? "Warning" : "Info"}</span>
                {a.model && <span className="font-mono text-gray-100"> · {a.model}</span>}
                <span className="text-gray-300"> — {a.message}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
