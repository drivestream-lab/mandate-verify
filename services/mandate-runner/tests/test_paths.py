import pytest

from mandate_runner.paths import compile_pattern, first_match, tool_settings_changed


@pytest.mark.parametrize(
    ("pattern", "path", "hit"),
    [
        ("CLAUDE.md", "CLAUDE.md", True),
        ("CLAUDE.md", "docs/CLAUDE.md", False),
        ("Makefile", "Makefile.bak", False),
        (".claude/agents/**", ".claude/agents/builder.md", True),
        (".claude/agents/**", ".claude/agents", False),
        (".cursor/rules", ".cursor/rules", True),
        ("src/**/ledger/**", "src/engine/ledger/hash_chain.py", True),
        ("src/**/ledger/**", "src/database/postgres/repository/ledger/ledger_repository.py", True),
        ("src/**/ledger/**", "src/ledger/x.py", True),
        ("src/**/ledger/**", "src/engine/ledgerish/x.py", False),
        ("tests/**/*ledger*", "tests/properties/test_ledger_chain_detects_any_edit_or_deletion.py", True),
        ("tests/**/*ledger*", "tests/unit/ledger_x/test_y.py", False),
        (
            "src/business_services/gates/signoff*_service.py",
            "src/business_services/gates/signoff_voiding_service.py",
            True,
        ),
        (
            "src/business_services/gates/signoff*_service.py",
            "src/business_services/gates/sealed_call_service.py",
            False,
        ),
        (
            "tests/journeys/**/*merge*",
            "tests/journeys/release_a_wave/test_wave_merge_allowed_only_on_the_verified_head.py",
            True,
        ),
        (
            "tests/journeys/**/*evidence*",
            "tests/journeys/release_a_wave/test_evidence_from_an_edited_caller_is_rejected.py",
            True,
        ),
        ("docs/specification/adr/**", "docs/specification/adr/ADR-001.md", True),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("a.b", "axb", False),
    ],
)
def test_pattern(pattern, path, hit):
    assert bool(compile_pattern(pattern).match(path)) is hit


def test_first_match_returns_the_path():
    assert first_match(["README.md", "Makefile"], ["Makefile"]) == "Makefile"
    assert first_match(["README.md"], ["Makefile"]) is None


BASE = b'[project]\nname = "m"\nversion = "1"\n[tool.ruff]\nline-length = 120\n'


def test_project_metadata_change_is_not_a_tool_change():
    assert not tool_settings_changed(BASE, BASE.replace(b'version = "1"', b'version = "2"'))


def test_tool_table_change_is_detected():
    assert tool_settings_changed(BASE, BASE.replace(b"120", b"200"))


def test_new_tool_table_is_detected():
    assert tool_settings_changed(BASE, BASE + b"[tool.pyright]\nstrict = []\n")


def test_pyproject_added_or_removed_with_tools_is_detected():
    assert tool_settings_changed(None, BASE)
    assert tool_settings_changed(BASE, None)


def test_unparseable_pyproject_counts_as_changed():
    assert tool_settings_changed(BASE, b"[tool\n")
