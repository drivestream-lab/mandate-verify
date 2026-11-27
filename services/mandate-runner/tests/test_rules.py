"""Every review rule in PL-16 as the runner encodes it."""

from conftest import CHALLENGER, HEAD, KD, OLD, diff, first, pr, review, second, signoff

from mandate_runner.rules import evaluate, fit_description

PLAIN = diff("src/business_services/programmes/programme_readiness_service.py")
LEDGER = diff("src/engine/ledger/hash_chain.py")


def rule(cfg, p, reviews, d=PLAIN):
    v = evaluate(p, reviews, d, cfg)
    return "ok" if v.ok else v.rule


# --- challenger Approve on the head ------------------------------------------


def test_task_pr_with_challenger_approve_on_head_passes(cfg):
    assert rule(cfg, pr(), [first()]) == "ok"


def test_task_pr_without_any_review_fails(cfg):
    v = evaluate(pr(), [], PLAIN, cfg)
    assert v.rule == "challenger-approve"
    assert "mandate-challenger" in v.description


def test_challenger_approve_on_an_older_commit_does_not_count(cfg):
    assert rule(cfg, pr(), [first(commit=OLD)]) == "challenger-approve"


def test_approve_from_another_identity_does_not_count(cfg):
    assert rule(cfg, pr(), [review("mandate-builder[bot]", body="Challenger: first")]) == "challenger-approve"
    assert rule(cfg, pr(), [review(KD, body="Challenger: first")]) == "challenger-approve"


def test_challenger_approve_needs_its_marker(cfg):
    assert rule(cfg, pr(), [review(CHALLENGER, body="LGTM")]) == "challenger-approve"


def test_marker_must_be_a_whole_line(cfg):
    assert rule(cfg, pr(), [review(CHALLENGER, body="not Challenger: first at all")]) == "challenger-approve"


def test_later_changes_requested_replaces_the_approve(cfg):
    assert rule(cfg, pr(), [first(), first("CHANGES_REQUESTED")]) == "challenger-approve"


def test_dismissed_approve_does_not_count(cfg):
    assert rule(cfg, pr(), [first("DISMISSED")]) == "challenger-approve"


def test_later_comment_does_not_cancel_the_approve(cfg):
    assert rule(cfg, pr(), [first(), first("COMMENTED")]) == "ok"


def test_approve_after_changes_requested_counts(cfg):
    assert rule(cfg, pr(), [first("CHANGES_REQUESTED"), first()]) == "ok"


def test_second_marker_alone_does_not_satisfy_the_first(cfg):
    assert rule(cfg, pr(), [second()]) == "challenger-approve"


# --- second challenger on trust-critical paths --------------------------------


def test_trust_critical_path_needs_second_challenger(cfg):
    v = evaluate(pr(), [first()], LEDGER, cfg)
    assert v.rule == "second-challenger-approve"
    assert "src/engine/ledger/hash_chain.py" in v.description


def test_trust_critical_path_with_both_challengers_passes(cfg):
    assert rule(cfg, pr(), [first(), second()], LEDGER) == "ok"


def test_one_review_carrying_both_markers_counts_for_neither(cfg):
    both = review(CHALLENGER, body="Challenger: first\nChallenger: second")
    assert rule(cfg, pr(), [both], LEDGER) == "challenger-approve"
    assert rule(cfg, pr(), [first(), both], LEDGER) == "second-challenger-approve"


def test_second_challenger_on_an_older_commit_does_not_count(cfg):
    assert rule(cfg, pr(), [first(), second(commit=OLD)], LEDGER) == "second-challenger-approve"


def test_second_challenger_changes_requested_fails(cfg):
    assert rule(cfg, pr(), [first(), second(), second("CHANGES_REQUESTED")], LEDGER) == "second-challenger-approve"


def test_each_trust_critical_capability_needs_the_second_challenger(cfg):
    for path in (
        "src/business_services/ledger/ledger_append_service.py",
        "src/models/ledger_models.py",
        "tests/unit/business_services/ledger/test_ledger_append_service.py",
        "src/business_services/gates/signoff_service.py",
        "src/business_services/gates/gate_decision_check_service.py",
        "src/models/signoff_models.py",
        "src/infra_services/attestation_service.py",
        "src/business_services/release/evidence_verification_service.py",
        "src/models/evidence_models.py",
        "src/business_services/release/merge_service.py",
        "src/business_services/release/unguarded_merge_sweep_service.py",
        "src/infra_services/github_app_service.py",
    ):
        assert rule(cfg, pr(), [first()], diff(path)) == "second-challenger-approve", path


def test_control_tower_never_needs_a_second_challenger(cfg):
    ct = pr(repo="drivestream-lab/mandate-control-tower")
    assert rule(cfg, ct, [first()], diff("src/ledger/view.ts")) == "ok"


# --- protected paths: a second, separate challenger Approve --------------------


def test_protected_path_needs_a_second_challenger(cfg):
    v = evaluate(pr(), [first()], diff("CLAUDE.md"), cfg)
    assert v.rule == "protected-second-approve"
    assert "CLAUDE.md" in v.description and "Challenger: second" in v.description


def test_protected_path_with_first_and_second_passes(cfg):
    assert rule(cfg, pr(), [first(), second()], diff("CLAUDE.md")) == "ok"


def test_no_persons_approve_counts_on_a_protected_path(cfg):
    assert rule(cfg, pr(), [first(), review(KD)], diff("Makefile")) == "protected-second-approve"
    assert (
        rule(cfg, pr(), [first(), review(KD, body="Challenger: second")], diff("Makefile"))
        == "protected-second-approve"
    )


def test_second_approve_on_an_older_commit_does_not_count(cfg):
    assert rule(cfg, pr(), [first(), second(commit=OLD)], diff("Makefile")) == "protected-second-approve"


def test_second_changes_requested_after_approve_fails(cfg):
    reviews = [first(), second(), second("CHANGES_REQUESTED")]
    assert rule(cfg, pr(), reviews, diff("Makefile")) == "protected-second-approve"


def test_one_review_with_both_markers_counts_for_neither(cfg):
    both = review(CHALLENGER, body="Challenger: first\nChallenger: second")
    assert rule(cfg, pr(), [both], diff("CLAUDE.md")) == "challenger-approve"


def test_second_alone_does_not_replace_the_first(cfg):
    assert rule(cfg, pr(), [second()], diff("CLAUDE.md")) == "challenger-approve"


def test_every_pl16_protected_path_needs_a_second_challenger(cfg):
    for path in (
        "CLAUDE.md", ".claude/agents/builder.md", ".cursor/rules", "docs/specification/adr/ADR-001.md",
        "docs/specification/product/requirements.md", "tests/journeys/conftest.py", "tests/verify/test_x.py",
        "Makefile", ".importlinter", ".pre-commit-config.yaml",
    ):  # fmt: skip
        assert rule(cfg, pr(), [first()], diff(path)) == "protected-second-approve", path


def test_pyproject_tool_settings_need_a_second_challenger(cfg):
    v = evaluate(pr(), [first()], diff("pyproject.toml", tool_changed=True), cfg)
    assert v.rule == "protected-second-approve"
    assert "pyproject.toml [tool]" in v.description


def test_pyproject_non_tool_change_needs_only_the_first(cfg):
    assert rule(cfg, pr(), [first()], diff("pyproject.toml", tool_changed=False)) == "ok"


def test_trust_critical_and_protected_pass_with_first_and_second(cfg):
    d = diff("src/engine/ledger/hash_chain.py", "tests/journeys/release_a_wave/test_merge_x.py")
    assert rule(cfg, pr(), [first()], d) == "second-challenger-approve"
    assert rule(cfg, pr(), [first(), second()], d) == "ok"


# --- wave PR: develop into main, signed by the signer App ---------------------

ATTESTED = f"Accepting the wave.\n\nInitiative: INIT-MANDATE-001\nHead: {HEAD}\n"


def wave(**kw):
    kw.setdefault("labels", ("wave-accepted",))
    kw.setdefault("head_ref", "develop")
    return pr(base="main", **kw)


def test_signer_is_an_app_not_a_person(cfg):
    assert cfg.identities.signer_login == "mandate-challenger[bot]"


def test_signed_wave_pr_passes(cfg):
    assert rule(cfg, wave(), [signoff(ATTESTED)], LEDGER) == "ok"


def test_wave_pr_needs_no_challenger(cfg):
    assert rule(cfg, wave(), [signoff(ATTESTED)], diff("CLAUDE.md")) == "ok"


def test_wave_pr_from_another_branch_fails(cfg):
    assert rule(cfg, wave(head_ref="feature/x"), [signoff(ATTESTED)]) == "wave-source"


def test_wave_pr_from_a_fork_fails(cfg):
    assert rule(cfg, wave(head_repo="someone/mandate"), [signoff(ATTESTED)]) == "wave-source"


def test_wave_pr_needs_the_label(cfg):
    assert rule(cfg, wave(labels=()), [signoff(ATTESTED)]) == "wave-label"
    assert rule(cfg, wave(labels=("spec-lgtm",)), [signoff(ATTESTED)]) == "wave-label"


def test_wave_pr_must_be_ready(cfg):
    assert rule(cfg, wave(draft=True), [signoff(ATTESTED)]) == "wave-ready"


def test_wave_pr_needs_the_signoff(cfg):
    assert rule(cfg, wave(), []) == "wave-signer-approve"
    assert rule(cfg, wave(), [first(), second()]) == "wave-signer-approve"
    assert rule(cfg, wave(), [signoff(ATTESTED, commit=OLD)]) == "wave-signer-approve"
    assert rule(cfg, wave(), [signoff(ATTESTED, state="COMMENTED")]) == "wave-signer-approve"
    assert rule(cfg, wave(), [signoff(ATTESTED), signoff(ATTESTED, state="CHANGES_REQUESTED")]) == "wave-signer-approve"


def test_challenger_approve_without_the_signoff_marker_does_not_sign(cfg):
    assert rule(cfg, wave(), [review(CHALLENGER, body=ATTESTED)]) == "wave-signer-approve"
    assert rule(cfg, wave(), [review(CHALLENGER, body="Challenger: first\n" + ATTESTED)]) == "wave-signer-approve"


def test_signoff_with_another_marker_counts_for_neither(cfg):
    assert rule(cfg, wave(), [signoff("Challenger: second\n" + ATTESTED)]) == "wave-signer-approve"


def test_no_persons_signoff_counts(cfg):
    assert rule(cfg, wave(), [signoff(ATTESTED, login=KD)]) == "wave-signer-approve"
    assert rule(cfg, wave(), [signoff(ATTESTED, login="mandate-builder[bot]")]) == "wave-signer-approve"


def test_wave_signoff_without_attestation_fails(cfg):
    assert rule(cfg, wave(), [signoff("LGTM")]) == "wave-attestation"


def test_wave_attestation_must_name_this_head(cfg):
    stale = f"Initiative: INIT-MANDATE-001\nHead: {OLD}"
    short = f"Initiative: INIT-MANDATE-001\nHead: {HEAD[:12]}"
    assert rule(cfg, wave(), [signoff(stale)]) == "wave-attestation"
    assert rule(cfg, wave(), [signoff(short)]) == "wave-attestation"


def test_wave_attestation_must_name_the_initiative(cfg):
    other = f"Initiative: INIT-OTHER-001\nHead: {HEAD}"
    assert rule(cfg, wave(), [signoff(other)]) == "wave-attestation"


def test_descriptions_fit_githubs_limit(cfg):
    long_path = "src/" + "x/" * 100 + "ledger/a.py"
    v = evaluate(pr(), [first()], diff(long_path), cfg)
    assert len(fit_description(v.description)) <= 140
    assert fit_description("a  b\nc") == "a b c"
