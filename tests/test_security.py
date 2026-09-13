import pytest

from agentic_journal.security import redact_value


def _token(*parts: str) -> str:
    return "".join(parts)


def test_redacts_known_secret_keys():
    value = redact_value({"OPENAI_API_KEY": "sk-secret", "safe": "ok"})

    assert value["OPENAI_API_KEY"] == "[REDACTED]"
    assert value["safe"] == "ok"


def test_preserves_numeric_token_usage_metadata():
    value = redact_value(
        {
            "evidence": {
                "token_usage": {
                    "input_tokens": 1200,
                    "output_tokens": 340,
                },
                "api_token": "secret-value",
            }
        }
    )

    assert value["evidence"]["token_usage"] == {"input_tokens": 1200, "output_tokens": 340}
    assert value["evidence"]["api_token"] == "[REDACTED]"


def test_redacts_bearer_tokens_in_strings():
    value = redact_value("Authorization: Bearer abcdef123456")

    assert "abcdef123456" not in value
    assert "[REDACTED]" in value


def test_redacts_nested_values():
    value = redact_value({"semantic": {"note": "GEMINI_API_KEY=secret-value"}})

    assert "secret-value" not in value["semantic"]["note"]


@pytest.mark.parametrize(
    "secret",
    [
        _token("AKIA", "IOSFODNN7EXAMPLE"),  # AWS access key id
        _token("ghp_", "16C7e42F292c6912E7710c838347Ae178B4a"),  # GitHub classic PAT
        _token("gho_", "16C7e42F292c6912E7710c838347Ae178B4a"),  # GitHub OAuth
        _token("github_pat_", "11ABCDEFG0123456789_abcdefghijklmnop"),  # GitHub fine-grained
        _token("glpat-", "ABC123def456GHI789jklm"),  # GitLab PAT
        _token("xoxb-", "2345678901-ABCDEFghijkl"),  # Slack bot token
        _token("AIza", "SyA1234567890abcdefghijklmnopqrstuv"),  # Google API key
        _token(
            "eyJhbGciOiJIUzI1NiJ9.",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0.",
            "dozjgNryP4J3jVmNHl0w5N",
        ),  # JWT
        _token("sk_live_", "51AbCdEfGhIjKlMnOpQrStUv"),  # Stripe live key (underscore)
        _token("npm_", "abcdefghijklmnopqrstuvwxyz1234567890"),  # npm automation token
        _token("AC", "0123456789abcdef0123456789abcdef"),  # Twilio account SID
    ],
)
def test_redacts_common_token_formats_in_free_text(secret):
    # A secret embedded in a free-text field with no secret-looking key must
    # still be redacted by value-shape detection.
    value = redact_value(f"deployed using {secret} just now")

    assert secret not in value
    assert "[REDACTED]" in value


def test_redacts_azure_account_key_assignment():
    value = redact_value("DefaultEndpointsProtocol=https;AccountName=app;AccountKey=abcdefghijklmnopqrstuvwxyz1234567890+/=;EndpointSuffix=core.windows.net")

    assert "abcdefghijklmnopqrstuvwxyz1234567890+/=" not in value
    assert "AccountName=app" in value
    assert "EndpointSuffix=core.windows.net" in value


def test_redacts_headless_ssh_rsa_key_material():
    key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCabcdefghi0123456789"
    value = redact_value(f"authorized key: {key}")

    assert "AAAAB3NzaC1yc2EAAAADAQABAAABAQCabcdefghi0123456789" not in value
    assert "[REDACTED]" in value


def test_redacts_long_hex_secret_assignment_without_hiding_commit_like_text():
    secret = "0123456789abcdef0123456789abcdef01234567"

    value = redact_value(f"api_key={secret}; commit {secret}")

    assert f"api_key={secret}" not in value
    assert f"commit {secret}" in value


def test_redacts_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"

    assert "MIIEpAIBAAKCAQEA" not in redact_value(pem)


def test_redacts_url_embedded_credentials():
    value = redact_value("postgres://admin:S3cr3tPass@db.internal:5432/app")

    assert "S3cr3tPass" not in value
    assert "admin" in value  # username is not a secret


def test_assignment_redaction_preserves_trailing_content():
    # The value group must stop at the delimiter so non-secret content survives.
    value = redact_value("api_key=abcd1234&user=alice")

    assert "abcd1234" not in value
    assert "user=alice" in value


def test_assignment_redaction_handles_quoted_value_with_spaces():
    value = redact_value('PASSWORD="hunter2 with space"')

    assert "hunter2 with space" not in value


def test_does_not_redact_plain_prefixed_identifiers():
    value = redact_value("pk_test_widget_render_ok rk_unit_test_helper")

    assert value == "pk_test_widget_render_ok rk_unit_test_helper"


def test_redacts_provider_specific_prefixed_secrets():
    live_secret = _token("sk_live_", "51AbCdEfGhIjKlMnOpQrStUv")
    publishable_secret = _token("pk_live_", "51AbCdEfGhIjKlMnOpQrStUv")
    openai_secret = _token("sk-", "this-is-a-long-secret-token")

    value = redact_value(f"{live_secret} {publishable_secret} {openai_secret}")

    assert live_secret not in value
    assert publishable_secret not in value
    assert openai_secret not in value


def test_bare_bearer_does_not_redact_plain_words():
    assert redact_value("Bearer responsibility matters") == "Bearer responsibility matters"


def test_bare_bearer_redacts_token_shaped_value():
    value = redact_value("Bearer abcdefghijklmnopqr12")

    assert value == "Bearer [REDACTED]"


def test_redacts_quoted_json_secret_assignments_in_free_text():
    value = redact_value('curl -d {"password":"hunter2plaintext","api_key":"abc123secret"}')

    assert "hunter2plaintext" not in value
    assert "abc123secret" not in value
    assert value.count("[REDACTED]") == 2


def test_redaction_is_linear_on_long_dotted_strings():
    # A long run of scheme-class characters with no "://" must not trigger
    # super-linear backtracking in the URL-credential pattern (ReDoS guard).
    import time

    payload = "java -cp " + ".".join(["com.example.module"] * 16000)

    start = time.perf_counter()
    result = redact_value(payload)
    elapsed = time.perf_counter() - start

    assert result == payload  # nothing secret-shaped to redact
    assert elapsed < 3.0, f"redaction took {elapsed:.2f}s (possible ReDoS)"


def test_redact_value_keeps_extended_numeric_token_usage_counters():
    from agentic_journal.security import redact_value

    usage = {
        "input_tokens": 1,
        "cached_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "cache_write_input_tokens": 4,
        "reasoning_output_tokens": 5,
        "total_tokens": 6,
    }

    assert redact_value({"evidence": {"token_usage": usage}}) == {"evidence": {"token_usage": usage}}
