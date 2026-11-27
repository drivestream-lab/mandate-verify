import pytest

from mandate_runner import config as config_mod


def test_real_config_pins_pl16_identities_and_commands(cfg):
    assert cfg.poll_interval_seconds == 30
    assert cfg.status_context == "mandate/build-verified"
    assert cfg.identities.runner_app_id == 5860839
    assert cfg.identities.challenger_app_id == 5860827
    assert cfg.identities.signer_login == "mandate-challenger[bot]"
    assert cfg.markers.signoff == "Signoff: wave"
    assert {r.name for r in cfg.repos} == {"drivestream-lab/mandate", "drivestream-lab/mandate-control-tower"}
    for base in ("develop", "main"):
        assert cfg.bases[base].commands == ("make setup", "make check", "make test", "make deploy", "make verify")


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
                       "challenger_login": "c[bot]", "signer_login": "c[bot]", "runner_login": "r[bot]"},
        "markers": {"first": "Challenger: first", "second": "Challenger: second", "signoff": "Signoff: wave"},
        "wave": {"base_branch": "main", "head_branch": "develop", "label": "wave-accepted", "initiative": "I"},
        "bases": {"main": {"commands": ["make test"]}},
        "repos": [{"name": "o/r", "protected_paths": [], "pyproject_tool_settings_protected": True,
                   "trust_critical_paths": []}],
        "sandbox": {"rebuild_interval_hours": 24, "rebuild_after_failure": True},
    }  # fmt: skip


def _develop_run() -> dict:
    return {
        "repo": "o/r", "branch": "develop", "task_base": "main", "commands": ["make journeys"],
        "freeze_label": "develop-red",
        "deploy": {"enabled": True, "container": "lab-mandate", "network": "lab_local", "image_repo": "o/r",
                   "env_file": "/config/mandate.env", "database": "mandate", "health_url": "http://l/health",
                   "service_url": "http://lab-mandate:8000/"},
    }  # fmt: skip


def test_real_config_runs_develop_on_mandate_and_deploys_it(cfg):
    dr = cfg.develop_run
    assert dr is not None
    assert (dr.repo, dr.branch, dr.task_base) == ("drivestream-lab/mandate", "develop", "develop")
    assert dr.commands == ("make setup", "make verify")
    assert dr.freeze_label == "develop-red"
    assert dr.deploy.enabled and dr.deploy.container == "lab-mandate" and dr.deploy.network == "lab_local"
    assert dr.deploy.database == "mandate"
    assert dr.deploy.service_url == "http://lab-mandate:8000"
    assert cfg.identities.runner_login == "mandate-runner[bot]"


def test_real_config_rebuilds_the_sandbox_daily_and_after_failure(cfg):
    assert cfg.sandbox.rebuild_interval_hours == 24
    assert cfg.sandbox.rebuild_after_failure


def test_develop_run_is_optional():
    assert config_mod.parse(_minimal()).develop_run is None


def test_develop_run_task_base_must_have_a_command_set():
    raw = _minimal()
    raw["develop_run"] = {**_develop_run(), "task_base": "develop"}
    with pytest.raises(config_mod.ConfigError, match="task_base"):
        config_mod.parse(raw)


def test_develop_run_needs_commands():
    raw = _minimal()
    raw["develop_run"] = {**_develop_run(), "commands": []}
    with pytest.raises(config_mod.ConfigError, match="commands"):
        config_mod.parse(raw)


def test_sandbox_rebuild_interval_must_be_at_least_an_hour():
    raw = _minimal()
    raw["sandbox"]["rebuild_interval_hours"] = 0
    with pytest.raises(config_mod.ConfigError, match="rebuild_interval_hours"):
        config_mod.parse(raw)


def test_config_without_a_wave_command_set_is_refused():
    raw = _minimal()
    raw["bases"] = {"develop": {"commands": ["make test"]}}
    with pytest.raises(config_mod.ConfigError, match="wave base"):
        config_mod.parse(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [("second", "Challenger: first"), ("signoff", "Challenger: first"), ("signoff", " Challenger: second ")],
)
def test_config_with_identical_markers_is_refused(key, value):
    raw = _minimal()
    raw["markers"][key] = value
    with pytest.raises(config_mod.ConfigError, match="markers"):
        config_mod.parse(raw)


@pytest.mark.parametrize("key", ["first", "second", "signoff"])
@pytest.mark.parametrize("value", ["", "   "])
def test_config_with_an_empty_marker_is_refused(key, value):
    raw = _minimal()
    raw["markers"][key] = value
    with pytest.raises(config_mod.ConfigError, match="empty"):
        config_mod.parse(raw)


@pytest.mark.parametrize("signer", ["nikd10x", "0xbeefdead", "mandate-builder[bot]", "r[bot]"])
def test_config_with_a_signer_other_than_the_challenger_app_is_refused(signer):
    raw = _minimal()
    raw["identities"]["signer_login"] = signer
    with pytest.raises(config_mod.ConfigError, match="signer_login"):
        config_mod.parse(raw)


def test_config_with_empty_command_set_is_refused():
    raw = _minimal()
    raw["bases"]["main"]["commands"] = []
    with pytest.raises(config_mod.ConfigError):
        config_mod.parse(raw)
