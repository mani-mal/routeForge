"""Weighted traffic personas + seeded synthetic PII generators.

Synthetic data only — every SSN/email/account number here is generated from a
seeded `random.Random`, never sourced from real customers. See
`tests/traffic/README.md` and the `traffic-and-tests` skill.

No Faker: hand-rolled generators keep this deterministic and offline, matching
the exact shapes `app.policy.detectors.RegexDetector` looks for.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

DEFAULT_PERSONAS_PATH = "tests/personas.yaml"

# A representative agent tool-output log line. Duplicated (not lengthened) to
# build the multi_step "large_tool_output" overlay — Headroom's compressor
# collapses repeated LINES, not merely long single lines, so duplication is
# what gives it material (see .claude/skills/headroom-compression).
_LOG_LINE = (
    'ts=2026-07-22T00:00:00Z level=INFO tool=search_docs status=ok '
    'took_ms=42 result="lorem ipsum dolor sit amet consectetur adipiscing"'
)
_LARGE_LOG_BLOCK = "\n".join([_LOG_LINE] * 400)


# ---------------------------------------------------------------------------
# Synthetic PII generators — seeded, deterministic, match RegexDetector's
# patterns exactly (app/policy/detectors.py _PATTERNS) so pii_bearing /
# high_risk personas actually trip the policy gate.
# ---------------------------------------------------------------------------

def fake_ssn(rng: random.Random) -> str:
    """Synthetic SSN shaped NNN-NN-NNNN (matches RegexDetector US_SSN)."""
    return (f"{rng.randint(0, 999):03d}-"
            f"{rng.randint(0, 99):02d}-"
            f"{rng.randint(0, 9999):04d}")


def fake_email(rng: random.Random) -> str:
    """Synthetic email (matches RegexDetector EMAIL_ADDRESS)."""
    user = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(8))
    domain = rng.choice(("example", "synthdemo", "fakemail"))
    tld = rng.choice(("com", "org", "net"))
    return f"{user}@{domain}.{tld}"


def fake_account(rng: random.Random) -> str:
    """Synthetic bank account number, 8-17 digits (matches RegexDetector
    US_BANK_NUMBER)."""
    length = rng.randint(8, 17)
    return "".join(str(rng.randint(0, 9)) for _ in range(length))


def fake_phone(rng: random.Random) -> str:
    """Synthetic phone number NNN-NNN-NNNN (matches RegexDetector
    PHONE_NUMBER)."""
    return (f"{rng.randint(200, 999)}-"
            f"{rng.randint(200, 999)}-"
            f"{rng.randint(1000, 9999)}")


# ---------------------------------------------------------------------------
# Persona body builders. Each returns a valid /v1/chat/completions request
# body. Registered by persona name below so tests/personas.yaml stays pure
# metadata (weight/api_key/expect_onprem) and this module owns the shapes.
# ---------------------------------------------------------------------------

_SIMPLE_QUESTIONS = (
    "What is the capital of France?",
    "What is 2 + 2?",
    "Name a primary color.",
    "What day comes after Monday?",
    "What is the boiling point of water in Celsius?",
)

_MODERATE_QUESTIONS = (
    "Summarize the main differences between TCP and UDP in a few sentences.",
    "Explain briefly why caching improves API latency.",
    "Give a short overview of how a hash table resolves collisions.",
    "Describe the tradeoffs between SQL and NoSQL databases briefly.",
)

_LOW_CONFIDENCE_QUESTIONS = (
    "What's an interesting fact about deep-sea creatures?",
    "Give me a fun trivia question about space exploration.",
    "What's a surprising fact about octopus cognition?",
)


def _build_simple_qa(rng: random.Random) -> dict:
    return {"messages": [{"role": "user", "content": rng.choice(_SIMPLE_QUESTIONS)}]}


def _build_moderate(rng: random.Random) -> dict:
    return {"messages": [{"role": "user", "content": rng.choice(_MODERATE_QUESTIONS)}]}


def _build_pii_bearing(rng: random.Random) -> dict:
    ssn = fake_ssn(rng)
    email = fake_email(rng)
    content = (
        f"My SSN is {ssn} and you can reach me at {email}. "
        "Please update my patient record with this contact info."
    )
    return {"messages": [{"role": "user", "content": content}]}


def _build_multi_step(rng: random.Random) -> dict:
    # large_tool_output overlay: the gate collapses all messages into a
    # single sanitized message (app/pipeline.py joins message content), so
    # the duplicated log block lives inside the one user message rather than
    # a separate tool-role message — keeps the last message role "user" for
    # the downstream agent runtime while still giving Headroom real
    # duplicate-line material to compress.
    content = (
        "Look up the latest deployment status step by step using the tool "
        "log below, then summarize it.\n\n" + _LARGE_LOG_BLOCK
    )
    return {
        "messages": [{"role": "user", "content": content}],
        "mock_multistep": True,
    }


def _build_high_risk(rng: random.Random) -> dict:
    # Uses a synthetic bank account number (US_BANK_NUMBER) rather than a
    # medical-license token: MEDICAL_LICENSE is presidio-only and not
    # recognized by RegexDetector, so it wouldn't trip the gate offline.
    account = fake_account(rng)
    content = (
        f"Urgent compliance review: account number {account} shows "
        "irregular high-risk activity. Escalate immediately and flag the "
        "account pending investigation."
    )
    return {"messages": [{"role": "user", "content": content}]}


def _build_low_confidence(rng: random.Random) -> dict:
    return {
        "messages": [{"role": "user", "content": rng.choice(_LOW_CONFIDENCE_QUESTIONS)}],
        "mock_confidence": 0.1,
    }


_BUILDERS: dict[str, Callable[[random.Random], dict]] = {
    "simple_qa": _build_simple_qa,
    "moderate": _build_moderate,
    "pii_bearing": _build_pii_bearing,
    "multi_step": _build_multi_step,
    "high_risk": _build_high_risk,
    "low_confidence": _build_low_confidence,
}


@dataclass
class Persona:
    name: str
    weight: int
    api_key: str
    expect_onprem: bool
    _builder: Callable[[random.Random], dict] = field(repr=False)

    def build(self, rng: random.Random) -> dict:
        return self._builder(rng)


def load_personas(path: str | Path = DEFAULT_PERSONAS_PATH) -> list[Persona]:
    """Load the weighted persona mix from tests/personas.yaml (single source
    of the weights, per the traffic-and-tests skill)."""
    raw = yaml.safe_load(Path(path).read_text())
    personas: list[Persona] = []
    for entry in raw["personas"]:
        name = entry["name"]
        try:
            builder = _BUILDERS[name]
        except KeyError:
            raise ValueError(
                f"tests/traffic/personas.py has no body builder registered "
                f"for persona {name!r} (add one to _BUILDERS)"
            ) from None
        personas.append(Persona(
            name=name,
            weight=entry["weight"],
            api_key=entry["api_key"],
            expect_onprem=entry["expect_onprem"],
            _builder=builder,
        ))
    return personas


def sample(personas: list[Persona], rng: random.Random, n: int) -> list[Persona]:
    """Weighted sample of n personas, deterministic given rng's state."""
    weights = [p.weight for p in personas]
    return rng.choices(personas, weights=weights, k=n)
