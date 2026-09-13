from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Dict keys whose value is always a secret regardless of its shape.
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|account[_-]?key|token|secret|password|passwd|authorization|credential|private[_-]?key)",
    re.IGNORECASE,
)
TOKEN_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
    "reasoning_output_tokens",
    "total_tokens",
}

# Secret-named assignments: `API_KEY=...`, `DB_PASSWORD: "..."`, `AccountKey=...`.
# The value group is quote-aware and stops at common delimiters (& ; , whitespace)
# so trailing non-secret content (e.g. `&user=alice`) is preserved instead of
# being swallowed. The separator is captured and re-emitted unchanged.
ASSIGNMENT_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])([\"']?)"
    r"([A-Za-z0-9_-]*(?:API[_-]?KEY|ACCOUNT[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD)[A-Za-z0-9_-]*)"
    r"(\1\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s&;,\"'}]+)",
    re.IGNORECASE,
)

# Value-shape patterns. These fire on the credential format itself, so a secret
# reaches redaction even when it sits in a free-text field (summary/note/command)
# with no secret-looking key around it. Each tuple is (pattern, replacement).
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private key blocks (whole block, multiline).
    (
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # Bare BEGIN marker without a matching END.
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"), REDACTED),
    # `Authorization: Bearer <token>` and bare `Bearer <token>`.
    (re.compile(r"(Authorization:\s*Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1" + REDACTED),
    (
        re.compile(r"\bBearer\s+(?=[A-Za-z0-9._~+/=-]{20,}\b)(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]+\b"),
        "Bearer " + REDACTED,
    ),
    # AWS access key id.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    # GitHub tokens (classic PAT/OAuth/user/server/refresh + fine-grained).
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    # GitLab personal access token.
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), REDACTED),
    # npm automation/auth tokens.
    (re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"), REDACTED),
    # Slack tokens.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    # Twilio account SID.
    (re.compile(r"\bAC[0-9a-fA-F]{32}\b"), REDACTED),
    # Google API key.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    # JWT (header.payload.signature).
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), REDACTED),
    # Stripe/OpenAI style keys with provider-specific infixes or long sk- tokens.
    (re.compile(r"\b(?:sk_live|sk_test|pk_live|rk_live)_[A-Za-z0-9_-]{10,}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), REDACTED),
    # Headless OpenSSH public-key material.
    (re.compile(r"\bssh-rsa\s+[A-Za-z0-9+/=]{32,}\b"), "ssh-rsa " + REDACTED),
    # Credentials embedded in a connection string URL: scheme://user:password@host
    # The scheme and value lengths are bounded so a long run of scheme-class
    # characters with no "://" cannot cause super-linear (ReDoS) backtracking.
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]{0,30}://[^\s:/@]+):([^\s/@]{1,256})@"), r"\1:" + REDACTED + "@"),
)


def _redact_assignment(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}"


def _redact_string(value: str) -> str:
    value = ASSIGNMENT_SECRET_RE.sub(_redact_assignment, value)
    for pattern, replacement in _VALUE_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _is_safe_token_usage(key: str, value: Any) -> bool:
    if key.lower().replace("-", "_") != "token_usage" or not isinstance(value, Mapping):
        return False
    return all(
        str(child_key) in TOKEN_USAGE_KEYS
        and isinstance(child_value, int)
        and not isinstance(child_value, bool)
        for child_key, child_value in value.items()
    )


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                if _is_safe_token_usage(key_text, item):
                    redacted[key_text] = {str(child_key): child_value for child_key, child_value in item.items()}
                    continue
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value
