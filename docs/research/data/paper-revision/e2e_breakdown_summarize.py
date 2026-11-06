"""Derives the headline 'RouteForge own time' table from e2e_breakdown.out (parses the
captured text; runs nothing).  python e2e_breakdown_summarize.py > e2e_breakdown_summary.out

Per configuration: the per-stage MEDIAN exclusive times of the instrumented pass, summed over
non-database, non-stub components (labelled compute) and the unlabelled remainder (ASGI,
FastAPI validation, pydantic, JSON, event-loop scheduling). Sums of medians are an
approximation; means are shown beside them."""
import re
from pathlib import Path

L = (Path(__file__).parent / "e2e_breakdown.out").read_text().splitlines()
cfg = det = prompt = None
rows = {}
for l in L:
    if l.startswith("####"):
        cfg = re.match(r"#### database=(.*?)\s+detection=", l).group(1)
        det = re.search(r"detection=(\w+)", l).group(1)
    elif l.startswith("-- prompt"):
        prompt = l[10:]
        cur = {}
        rows[(cfg, det, prompt)] = cur
    else:
        m = re.match(r"\s+(\S.*?)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)%$", l)
        if m and prompt:
            cur[m.group(1).strip()] = (float(m.group(2)), float(m.group(3)))


def keep(n):
    return not n.startswith(("db", "store.", "unlabelled")) and "stub" not in n


print(f"{'database':44s} {'detect':9s} {'prompt':16s} {'labelled non-DB (med-sum / mean-sum)':>38s} "
      f"{'unlabelled (med/mean)':>22s} {'detection (med)':>16s} {'non-DB total (med-sum)':>23s}")
for (c, d, p), v in rows.items():
    nm = sum(m for n, (a, m) in v.items() if keep(n))
    na = sum(a for n, (a, m) in v.items() if keep(n))
    u = next(x for n, x in v.items() if n.startswith("unlab"))
    det_ms = v.get("detect.analyze", (0, 0))[1]
    print(f"{c[:44]:44s} {d:9s} {p:16s} {nm:17.2f} / {na:6.2f} ms {u[1]:10.2f} / {u[0]:5.2f} ms "
          f"{det_ms:13.2f} ms {nm + u[1]:20.2f} ms")
