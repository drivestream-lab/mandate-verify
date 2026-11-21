"""The runner's own config file. The only source of commands and review rules."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Identities:
    runner_app_id: int
    challenger_app_slug: str
    challenger_app_id: int
    challenger_login: str
    human_signer_login: str


@dataclass(frozen=True)
class Markers:
    first: str
    second: str


@dataclass(frozen=True)
class WaveRules:
    base_branch: str
    head_branch: str
    label: str
    initiative: str


@dataclass(frozen=True)
class BaseRules:
    name: str
    commands: tuple[str, ...]


@dataclass(frozen=True)
class RepoRules:
    name: str
    protected_paths: tuple[str, ...]
    pyproject_tool_settings_protected: bool
    trust_critical_paths: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    poll_interval_seconds: int
    status_context: str
    log_base_url: str
    data_dir: Path
    work_dir: Path
    job_image_tag: str
    job_image_context: Path
    command_timeout_seconds: int
    identities: Identities
    markers: Markers
    wave: WaveRules
    bases: dict[str, BaseRules]
    repos: tuple[RepoRules, ...]

    def repo(self, name: str) -> RepoRules:
        for repo in self.repos:
            if repo.name == name:
                return repo
        raise KeyError(name)


def _get(table: dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in table:
        raise ConfigError(f"{where}: missing '{key}'")
    value = table[key]
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be an integer")
    if not isinstance(value, kind):
        raise ConfigError(f"{where}: '{key}' must be {kind.__name__}")
    return value


def _strings(table: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    values = _get(table, key, list, where)
    if not all(isinstance(v, str) and v for v in values):
        raise ConfigError(f"{where}: '{key}' must be a list of non-empty strings")
    return tuple(values)


def parse(raw: dict[str, Any]) -> Config:
    ids = _get(raw, "identities", dict, "config")
    identities = Identities(
        runner_app_id=_get(ids, "runner_app_id", int, "identities"),
        challenger_app_slug=_get(ids, "challenger_app_slug", str, "identities"),
        challenger_app_id=_get(ids, "challenger_app_id", int, "identities"),
        challenger_login=_get(ids, "challenger_login", str, "identities"),
        human_signer_login=_get(ids, "human_signer_login", str, "identities"),
    )
    m = _get(raw, "markers", dict, "config")
    markers = Markers(first=_get(m, "first", str, "markers"), second=_get(m, "second", str, "markers"))
    if markers.first.strip() == markers.second.strip():
        raise ConfigError("markers: first and second must differ")
    w = _get(raw, "wave", dict, "config")
    wave = WaveRules(
        base_branch=_get(w, "base_branch", str, "wave"),
        head_branch=_get(w, "head_branch", str, "wave"),
        label=_get(w, "label", str, "wave"),
        initiative=_get(w, "initiative", str, "wave"),
    )
    bases: dict[str, BaseRules] = {}
    for name, table in _get(raw, "bases", dict, "config").items():
        commands = _strings(table, "commands", f"bases.{name}")
        if not commands:
            raise ConfigError(f"bases.{name}: commands must not be empty")
        bases[name] = BaseRules(name=name, commands=commands)
    if wave.base_branch not in bases:
        raise ConfigError(f"bases: no command set for the wave base '{wave.base_branch}'")
    repos: list[RepoRules] = []
    for i, table in enumerate(_get(raw, "repos", list, "config")):
        where = f"repos[{i}]"
        repos.append(
            RepoRules(
                name=_get(table, "name", str, where),
                protected_paths=_strings(table, "protected_paths", where),
                pyproject_tool_settings_protected=_get(table, "pyproject_tool_settings_protected", bool, where),
                trust_critical_paths=_strings(table, "trust_critical_paths", where),
            )
        )
    if not repos:
        raise ConfigError("repos: at least one repo is required")
    interval = _get(raw, "poll_interval_seconds", int, "config")
    if interval < 5:
        raise ConfigError("poll_interval_seconds must be at least 5")
    return Config(
        poll_interval_seconds=interval,
        status_context=_get(raw, "status_context", str, "config"),
        log_base_url=_get(raw, "log_base_url", str, "config").rstrip("/"),
        data_dir=Path(_get(raw, "data_dir", str, "config")),
        work_dir=Path(_get(raw, "work_dir", str, "config")),
        job_image_tag=_get(raw, "job_image_tag", str, "config"),
        job_image_context=Path(_get(raw, "job_image_context", str, "config")),
        command_timeout_seconds=_get(raw, "command_timeout_seconds", int, "config"),
        identities=identities,
        markers=markers,
        wave=wave,
        bases=bases,
        repos=tuple(repos),
    )


def load(path: Path) -> Config:
    with path.open("rb") as f:
        return parse(tomllib.load(f))
