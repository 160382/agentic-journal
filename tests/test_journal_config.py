import tomllib

from agentic_journal.config import DEFAULT_CONFIG, DEFAULTS, ensure_config, load_config


def test_defaults_match_generated_config_file():
    assert tomllib.loads(DEFAULT_CONFIG) == DEFAULTS


def test_load_config_without_file_returns_defaults(tmp_path, capsys):
    assert load_config(tmp_path) == DEFAULTS
    assert capsys.readouterr().err == ""


def test_load_config_overrides_defaults_and_keeps_extra_keys(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[privacy]\nlog_prompts = true\n\n[mcp]\ntools = ["journal_note"]\n',
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config["privacy"] == {**DEFAULTS["privacy"], "log_prompts": True}
    assert config["journal"] == DEFAULTS["journal"]
    assert config["mcp"] == {"tools": ["journal_note"]}


def test_load_config_does_not_mutate_defaults(tmp_path):
    (tmp_path / "config.toml").write_text("[privacy]\nlog_prompts = true\n", encoding="utf-8")

    load_config(tmp_path)

    assert DEFAULTS["privacy"]["log_prompts"] is False


def test_load_config_falls_back_to_defaults_on_invalid_toml(tmp_path, capsys):
    (tmp_path / "config.toml").write_text("[privacy\nlog_prompts = true\n", encoding="utf-8")

    assert load_config(tmp_path) == DEFAULTS
    assert "agentic-journal config: ignoring" in capsys.readouterr().err


def test_load_config_ignores_values_of_the_wrong_type(tmp_path, capsys):
    (tmp_path / "config.toml").write_text('[privacy]\nlog_prompts = "yes"\nredact_secrets = 0\n', encoding="utf-8")

    config = load_config(tmp_path)

    assert config["privacy"] == DEFAULTS["privacy"]
    err = capsys.readouterr().err
    assert "privacy.log_prompts" in err
    assert "privacy.redact_secrets" in err


def test_load_config_ignores_non_table_sections(tmp_path, capsys):
    (tmp_path / "config.toml").write_text("privacy = true\n", encoding="utf-8")

    assert load_config(tmp_path) == DEFAULTS
    assert "expected a table" in capsys.readouterr().err


def test_load_config_defaults_to_journal_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[privacy]\nlog_prompts = true\n", encoding="utf-8")

    assert load_config()["privacy"]["log_prompts"] is True


def test_ensure_config_keeps_existing_file(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[privacy]\nlog_prompts = true\n", encoding="utf-8")

    assert ensure_config(tmp_path) == config_path
    assert config_path.read_text(encoding="utf-8") == "[privacy]\nlog_prompts = true\n"
