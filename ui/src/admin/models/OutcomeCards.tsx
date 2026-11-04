import type { MetricsSummary } from "../../api";
import { change, insufficientText, money, percent } from "./format";
import type { Change } from "./format";

interface CardProps {
  title: string;
  tip: string;
  value: string;
  sub?: string;
  sub2?: string;
  delta: Change | null;
  insufficient?: boolean;
}

function Card({ title, tip, value, sub, sub2, delta, insufficient }: CardProps) {
  return (
    <div className="bg-panel border border-edge rounded-lg p-4 flex flex-col gap-1.5 min-w-0" title={tip}>
      <h3 className="text-[11px] font-display font-semibold uppercase tracking-[0.12em] text-gray-400 cursor-help">
        {title}
      </h3>
      <div className={insufficient ? "text-sm text-gray-400" : "text-2xl font-mono font-semibold text-gray-50"}>
        {value}
      </div>
      {sub && <div className="text-xs text-gray-300">{sub}</div>}
      {sub2 && <div className="text-[11px] text-gray-400">{sub2}</div>}
      <div className="text-xs text-gray-300 mt-auto pt-1">{delta ? delta.text : "no comparison available"}</div>
    </div>
  );
}

export function OutcomeCards({ data }: { data: MetricsSummary }) {
  const c = data.current;
  const p = data.previous;
  const unitWord = c.cost_per_task.task_unit === "request" ? "request" : "task";
  const s = c.savings;
  const rep = c.successful_reported;
  const costSucc = c.cost_per_task.successful;
  const asOf = data.baseline.as_of ? data.baseline.as_of : "undated";

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3">
      <Card
        title="Spend"
        tip="Total cost of all requests in the window, from usage records."
        value={money(c.spend.value)}
        sub={`${c.spend.n.toLocaleString()} requests`}
        delta={change(c.spend.value, p.spend.value)}
      />
      <Card
        title="Completed without failure"
        tip="Share of requests whose outcome was ok, over ok + backend_failed + verify_failed + context_overflow. Policy rejections and other statuses are not in the denominator."
        value={c.completed.value === null ? insufficientText(c.completed.n) : percent(c.completed.value)}
        insufficient={c.completed.value === null}
        sub="not a quality measure"
        sub2={`${c.completed.failures ?? 0} failures · ${
          c.completed.policy_rejections === null || c.completed.policy_rejections === undefined
            ? "policy rejections not attributable to a placement"
            : `${c.completed.policy_rejections} policy rejections`
        }`}
        delta={change(c.completed.value, p.completed.value)}
      />
      <Card
        title="Successful (reported)"
        tip="Share of tasks whose latest task_success feedback was positive. Only tasks with a report count; minimum 20 labelled tasks."
        value={
          rep === null
            ? "No reports yet: send task_success to /v1/feedback"
            : rep.value === null
              ? insufficientText(rep.n)
              : percent(rep.value)
        }
        insufficient={rep === null || rep.value === null}
        sub={rep ? `${rep.n} labelled tasks · ${rep.apps_reporting ?? 0} apps reporting` : undefined}
        delta={change(rep?.value ?? null, p.successful_reported?.value ?? null)}
      />
      <Card
        title="Cost per successful task"
        tip="Cost of labelled tasks divided by the number of successful labelled tasks. A task is a trajectory_key group; a request without one is its own task."
        value={
          costSucc !== null
            ? money(costSucc)
            : c.cost_per_task.successful_insufficient
              ? insufficientText(c.cost_per_task.n_labelled)
              : "no successful tasks reported"
        }
        insufficient={costSucc === null}
        sub={`attempted: ${money(c.cost_per_task.attempted)} per ${unitWord}`}
        delta={change(costSucc, p.cost_per_task.successful)}
      />
      <Card
        title="Savings"
        tip="Sum of the routing, prompt cache, compression and semantic cache savings over requests that were tracked. Requests that predate savings tracking are left out."
        value={money(s.total)}
        sub={`routing ${money(s.routing)} · prompt cache ${money(s.prompt_cache)} · compression ${money(
          s.compression,
        )} · semantic cache ${money(s.semantic_cache)}`}
        sub2={`every request priced at ${data.baseline.model} (prices as of ${asOf})`}
        delta={change(s.total, p.savings.total)}
      />
    </div>
  );
}
