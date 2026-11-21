import pytest

from mandate_runner import config as config_mod


def test_real_config_pins_pl16_identities_and_commands(cfg):
    assert cfg.poll_interval_seconds == 30
    assert cfg.status_context == "mandate/build-verified"
    assert cfg.identities.runner_app_id == 5860839
    assert cfg.identities.challenger_app_id == 5860827
    assert cfg.identities.human_signer_login == "nikd10x"
    assert {r.name for r in cfg.repos} == {"drivestream-lab/mandate", "drivestream-lab/mandate-control-tower"}
    for base in ("develop", "main"):
        assert cfg.bases[base].commands == ("make check", "make test", "make journeys")


def test_real_config_lists_every_pl16_protected_path(cfg):
    pl16 = {
        "CLAUDE.md", ".claude/agents/**", ".cursor/rules", "docs/specification/adr/**",
        "docs/specification/product/**", "tests/journeys/**", "tests/verify/**", "Makefile",
        ".importlinter", ".pre-commit-config.yaml",
    }  # fmt: skip
    for repo in cfg.repos:
        assert pl16 <= set(repo.protected_paths)
        assert repo.pyproject_tool_settings_protected


def test_control_tower_has_no_trust_critical_paths(cfg):
    assert cfg.repo("drivestream-lab/mandate-control-tower").trust_critical_paths == ()


def _minimal() -> dict:
    return {
        "poll_interval_seconds": 30, "status_context": "x", "log_base_url": "http://l", "data_dir": "/d",
        "work_dir": "/w", "job_image_tag": "j", "job_image_context": "/c", "command_timeout_seconds": 60,
        "identities": {"runner_app_id": 1, "challenger_app_slug": "c", "challenger_app_id": 2,
                       "challenger_login": "c[bot]", "human_signer_login": "kd"},
        "markers": {"first": "Challenger: first", "second": "Challenger: second"},
        "wave": {"base_branch": "main", "head_branch": "develop", "label": "wave-accepted", "initiative": "I"},
        "bases": {"main": {"commands": ["make test"]}},
        "repos": [{"name": "o/r", "protected_paths": [], "pyproject_tool_settings_protected": True,
                   "trust_critical_paths": []}],
    }  # fmt: skip


def test_config_without_a_wave_command_set_is_refused():
    raw = _minimal()
    raw["bases"] = {"develop": {"commands": ["make test"]}}
    with pytest.raises(config_mod.ConfigError, match="wave base"):
        config_mod.parse(raw)


def test_config_with_identical_markers_is_refused():
    raw = _minimal()
    raw["markers"]["second"] = "Challenger: first"
    with pytest.raises(config_mod.ConfigError, match="markers"):
        config_mod.parse(raw)


def test_config_with_empty_command_set_is_refused():
    raw = _minimal()
    raw["bases"]["main"]["commands"] = []
    with pytest.raises(config_mod.ConfigError):
        config_mod.parse(raw)
