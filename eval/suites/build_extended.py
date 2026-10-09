"""Generate eval/suites/extended.jsonl: a larger, well-posed FeasibleRouteBench suite.

    uv run python eval/suites/build_extended.py            # rewrite the suite
    uv run python eval/suites/build_extended.py --check    # fail if the file is stale

Why it exists. The 22-case starter suite has two placement groups below the
five-case minimum, every case is short enough to target the cheap tier (so no
scoring weight ever has a choice to make), and some of its cases cannot be
answered from the prompt. This suite is generated, so every case is answerable
from its own text, and it is balanced:

  placement   any | onprem_only | us_only | no_retention | escalated   (18 each; `any` 36)
  protection  enforced (90) | passthrough (18)
  workload    arithmetic | extraction | json | classification | reading | code   (18 each)
  length      short | medium | long  -- targets the cheap, mid and frontier tier
              under RouteForge's length-based rule (tokens ~ characters / 4)

108 cases. Deterministic: the same seed writes the same bytes. Synthetic text;
the SSN in the `escalated` cases is fabricated and exists to trigger detection.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

SEED = 20261009
OUT = Path(__file__).with_name("extended.jsonl")

WORDS = ("ledger audit visit schedule network referral appeal invoice coverage premium "
         "policy member provider deductible copay review window batch queue shard replica "
         "index cache tenant quota retry timeout budget forecast variance contract clause "
         "summary report record entry note draft outline section margin footer header").split()

PLACEMENTS = {
    "any":          ({"residency": "any"}, "enforced"),
    "onprem_only":  ({"residency": "onprem_only"}, "enforced"),
    "us_only":      ({"residency": "any", "allowed_regions": ["us"]}, "enforced"),
    "no_retention": ({"residency": "any", "require_no_retention": True}, "enforced"),
    "escalated":    ({"residency": "any"}, "enforced"),
    "any:passthrough": ({"residency": "any"}, "passthrough"),
}
# characters of unrelated background placed before the task
LENGTHS = {"short": 0, "medium": 4200, "long": 7600}


def background(rng: random.Random, chars: int) -> str:
    if not chars:
        return ""
    out, n = [], 0
    while n < chars:
        s = " ".join(rng.choice(WORDS) for _ in range(rng.randint(9, 16))).capitalize() + "."
        out.append(s)
        n += len(s) + 1
    return ("Background notes (not needed for the task below):\n" + " ".join(out)
            + "\n\nTask:\n")


def arithmetic(rng):
    a, b, c = rng.randint(12, 97), rng.randint(3, 19), rng.randint(10, 400)
    return (f"Compute {a} times {b}, then add {c}. Reply with the final number only, "
            "with no words and no working."), {"kind": "numeric", "expected": float(a * b + c)}


def extraction(rng):
    statuses = ["approved", "denied", "pending"]
    rows = [(f"CL-{rng.randint(1000, 9999)}", rng.choice(statuses), rng.randint(50, 9000))
            for _ in range(5)]
    cid, status, _ = rng.choice(rows)
    table = "\n".join(f"claim {i} | status {s} | amount {amt} dollars" for i, s, amt in rows)
    return (f"{table}\n\nWhat is the status of claim {cid}? Reply with one word.",
            {"kind": "contains", "expected": status})


def as_json(rng):
    cid, amount = f"CL-{rng.randint(1000, 9999)}", rng.randint(50, 9000)
    schema = {"type": "object", "required": ["claim_id", "amount"],
              "properties": {"claim_id": {"const": cid}, "amount": {"const": amount}}}
    return (f"A claim with identifier {cid} was filed for {amount} dollars. Return a JSON "
            'object with exactly two keys, "claim_id" (a string) and "amount" (a number). '
            "Return only the JSON object: no code fence and no other text.",
            {"kind": "json_schema", "expected": schema,
             "sample": json.dumps({"claim_id": cid, "amount": amount})})


def classification(rng):
    level = rng.choice(["ERROR", "WARN", "INFO"])
    svc, ms = rng.choice(WORDS), rng.randint(3, 8000)
    return (f"Log line: level={level} svc={svc} dur_ms={ms} msg=request finished\n\n"
            "Which level does this log line have: ERROR, WARN or INFO? Reply with that "
            "one word.", {"kind": "contains", "expected": level})


def reading(rng):
    days = rng.randint(3, 45)
    filler = [" ".join(rng.choice(WORDS) for _ in range(10)).capitalize() + "." for _ in range(4)]
    filler.insert(rng.randint(0, 4), f"The reconciliation window is {days} days.")
    return (" ".join(filler) + "\n\nHow many days is the reconciliation window? Reply with "
            "the number only.", {"kind": "numeric", "expected": float(days)})


def code(rng):
    k, m, x = rng.randint(2, 9), rng.randint(1, 20), rng.randint(2, 12)
    return (f"def f(x):\n    y = x * {k}\n    return y + {m}\n\nWhat does f({x}) return? "
            "Reply with the number only.", {"kind": "numeric", "expected": float(x * k + m)})


WORKLOADS = {"arithmetic": arithmetic, "extraction": extraction, "json": as_json,
             "classification": classification, "reading": reading, "code": code}


def build() -> list[dict]:
    rng = random.Random(SEED)
    cases = []
    for pkey, (policy, protection) in PLACEMENTS.items():
        placement = pkey.split(":")[0]
        for workload, make in WORKLOADS.items():
            for length, chars in LENGTHS.items():
                task, grader = make(rng)
                text = background(rng, chars) + task
                if placement == "escalated":
                    # A fabricated identifier the task does not depend on: it
                    # exists so detection raises this request's placement.
                    text = (f"Member reference: SSN {rng.randint(100, 665)}-"
                            f"{rng.randint(10, 99)}-{rng.randint(1000, 9999)}.\n\n" + text)
                app_policy = {"pii_detection": "off" if protection == "passthrough" else "regex",
                              "policy_mode": protection, **policy}
                cases.append({
                    "app_policy": dict(sorted(app_policy.items())),
                    "expect_empty": False,
                    "grader": grader,
                    "id": f"x-{placement}-{protection[:4]}-{workload}-{length}",
                    "messages": [{"content": text, "role": "user"}],
                    "tags": {"length": length, "placement": placement,
                             "protection": protection, "synthetic": True,
                             "workload": workload},
                })
    return cases


def render() -> str:
    return "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in build())


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == text else
                 "eval/suites/extended.jsonl is stale: run eval/suites/build_extended.py")
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(build())} cases)")
