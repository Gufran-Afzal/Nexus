"""Best-effort secret redaction for logs, audit records and captured command output."""

from __future__ import annotations

import re

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"),  # OpenAI/Stripe-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b"),  # Slack
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),  # Google API key
]

# KEY=value / "key": "value" style assignments where the key looks secret.
_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)[A-Z0-9_]*)"
    r"(\s*[:=]\s*[\"']?)([^\s\"']{4,})"
)

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _PATTERNS:
        text = pat.sub(REDACTED, text)
    return _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


SECRET_ENV_HINTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "ACCESS_KEY", "PRIVATE_KEY")


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    """Remove secret-looking environment variables before launching child processes."""
    return {k: v for k, v in env.items() if not any(h in k.upper() for h in SECRET_ENV_HINTS)}
