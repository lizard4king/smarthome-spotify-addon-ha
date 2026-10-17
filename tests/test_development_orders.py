from __future__ import annotations

import pytest
import unicodedata

from codex_voice.development_orders import (
    ADDON_REPOSITORY_CANDIDATES,
    SMART_HOME_REPOSITORY,
    ApprovalAction,
    DevelopmentOrderBook,
    DevelopmentOrderError,
    IssueReference,
    OrderStage,
    PlanStep,
    Project,
    RepositoryCatalog,
)


PERSON_A = "a" * 64
PERSON_B = "b" * 64


def advance_to_stage(instance, order_id, target):
    actions = [
        lambda: ready_for_execution(instance, order_id),
        lambda: instance.approve(order_id, ApprovalAction.EXECUTE, verified_person_key=PERSON_A),
        lambda: instance.start_execution(order_id, verified_person_key=PERSON_A),
        lambda: instance.finish_execution(order_id, verified_person_key=PERSON_A, success=True, summary="Local diff retained."),
        lambda: instance.record_tests(order_id, verified_person_key=PERSON_A, passed=True, summary="Tests passed."),
        lambda: instance.record_review(order_id, verified_person_key=PERSON_A, passed=True, summary="Review passed."),
        lambda: instance.approve(order_id, ApprovalAction.PUSH, verified_person_key=PERSON_A),
        lambda: instance.start_push(order_id, verified_person_key=PERSON_A),
        lambda: instance.mark_pushed(order_id, verified_person_key=PERSON_A),
        lambda: instance.approve(order_id, ApprovalAction.MERGE, verified_person_key=PERSON_A),
        lambda: instance.start_merge(order_id, verified_person_key=PERSON_A),
        lambda: instance.mark_merged(order_id, verified_person_key=PERSON_A),
        lambda: instance.approve(order_id, ApprovalAction.DEPLOY, verified_person_key=PERSON_A),
        lambda: instance.start_deploy(order_id, verified_person_key=PERSON_A),
        lambda: instance.mark_deployed(order_id, verified_person_key=PERSON_A),
    ]
    for action in actions:
        if instance.get(order_id, verified_person_key=PERSON_A).stage is target:
            return
        action()
    assert instance.get(order_id, verified_person_key=PERSON_A).stage is target


@pytest.mark.parametrize("stage", [
    OrderStage.AWAITING_TESTS, OrderStage.AWAITING_REVIEW, OrderStage.LOCAL_REVIEWED,
    OrderStage.PUSH_APPROVED, OrderStage.PUSHED, OrderStage.MERGE_APPROVED,
    OrderStage.MERGED, OrderStage.DEPLOY_APPROVED,
])
def test_idle_result_can_close_without_undoing_its_milestones(stage):
    instance = book()
    order, _ = submit(instance)
    advance_to_stage(instance, order.order_id, stage)
    before = instance.get(order.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError):
        instance.close_without_further_action(order.order_id, verified_person_key=PERSON_B)
    closed = instance.close_without_further_action(order.order_id, verified_person_key=PERSON_A)
    assert closed.stage is OrderStage.CLOSED
    assert closed.closed_after_stage is stage
    assert closed.execution_summary == before.execution_summary
    assert closed.test_summary == before.test_summary
    assert closed.review_summary == before.review_summary
    assert closed.approvals == before.approvals
    assert closed.pending_approval is None
    with pytest.raises(DevelopmentOrderError, match="terminal"):
        submit(instance)
    with pytest.raises(DevelopmentOrderError):
        instance.start_push(order.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError):
        instance.close_without_further_action(order.order_id, verified_person_key=PERSON_A)


@pytest.mark.parametrize("stage", [
    OrderStage.DRAFT, OrderStage.EXECUTING, OrderStage.PUSHING,
    OrderStage.MERGING, OrderStage.DEPLOYING, OrderStage.DEPLOYED,
])
def test_closing_never_masks_an_active_action_or_an_existing_terminal_state(stage):
    instance = book()
    order, _ = submit(instance)
    advance_to_stage(instance, order.order_id, stage)
    with pytest.raises(DevelopmentOrderError):
        instance.close_without_further_action(order.order_id, verified_person_key=PERSON_A)
    assert instance.get(order.order_id, verified_person_key=PERSON_A).stage is stage


def book(addon: str = "lizard4king/smarthome-spotify-addon-ha") -> DevelopmentOrderBook:
    return DevelopmentOrderBook(RepositoryCatalog(addon), {PERSON_A, PERSON_B})


def submit(
    instance: DevelopmentOrderBook | None = None,
    *,
    request_id: str = "request-1",
    issue_number: int = 60,
    project: Project = Project.SMART_HOME,
    goal: str = "Ergänze den geprüften Ablauf.",
    person: str = PERSON_A,
):
    return (instance or book()).submit(
        request_id=request_id,
        issue=IssueReference(SMART_HOME_REPOSITORY, issue_number),
        project=project,
        goal=goal,
        verified_person_key=person,
    )


def ready_for_execution(instance: DevelopmentOrderBook, order_id: str):
    return instance.set_plan(
        order_id,
        verified_person_key=PERSON_A,
        steps=(PlanStep("Implementieren", "Begrenzte Änderung", "Offline-Tests"),),
    )


def ready_for_review(instance: DevelopmentOrderBook, order_id: str):
    ready_for_execution(instance, order_id)
    instance.approve(order_id, ApprovalAction.EXECUTE, verified_person_key=PERSON_A)
    instance.start_execution(order_id, verified_person_key=PERSON_A)
    instance.finish_execution(order_id, verified_person_key=PERSON_A, success=True, summary="Lokale Änderung erstellt.")
    instance.record_tests(order_id, verified_person_key=PERSON_A, passed=True, summary="8 Tests bestanden.")
    return instance.record_review(order_id, verified_person_key=PERSON_A, passed=True, summary="Diff geprüft.")


def test_projects_resolve_only_to_verified_repository_slugs():
    assert ADDON_REPOSITORY_CANDIDATES == {"lizard4king/smarthome-spotify-addon-ha"}
    for addon in ADDON_REPOSITORY_CANDIDATES:
        catalog = RepositoryCatalog(addon)
        assert catalog.resolve(Project.SMART_HOME) == SMART_HOME_REPOSITORY
        assert catalog.resolve(Project.HA_ADDON) == addon
    with pytest.raises(DevelopmentOrderError):
        RepositoryCatalog("attacker/other")


def test_legacy_addon_repository_is_not_an_allowed_execution_target():
    with pytest.raises(DevelopmentOrderError, match="not allowlisted"):
        RepositoryCatalog("lizard4king/smarthome-spotify-addon")


def test_addon_repository_selection_remains_explicit():
    with pytest.raises(TypeError):
        RepositoryCatalog()


@pytest.mark.parametrize("repository,number", [
    ("attacker/SmartHome", 60),
    (SMART_HOME_REPOSITORY, 0),
    (SMART_HOME_REPOSITORY, True),
])
def test_issue_reference_rejects_nonallowlisted_or_invalid_issue(repository, number):
    with pytest.raises(DevelopmentOrderError):
        IssueReference(repository, number)


def test_submit_requires_authorized_verified_person_and_explicit_project():
    with pytest.raises(DevelopmentOrderError):
        book().submit(
            request_id="r", issue=IssueReference(SMART_HOME_REPOSITORY, 60),
            project=Project.SMART_HOME, goal="Fix it", verified_person_key="",
        )
    with pytest.raises(DevelopmentOrderError):
        submit(project="arbitrary-repo")


def test_duplicate_request_is_idempotent_but_changed_content_conflicts():
    instance = book()
    first, created = submit(instance)
    retry, created_retry = submit(instance)
    assert created is True
    assert created_retry is False
    assert retry == first
    assert first.branch_name == f"codex/{first.order_id}"
    whitespace_retry, created_again = submit(
        instance, goal="  Ergänze   den   geprüften Ablauf.  "
    )
    assert whitespace_retry == first
    assert created_again is False

    with pytest.raises(DevelopmentOrderError, match="different content"):
        submit(instance, goal="Different order", request_id="request-1")


def test_unknown_or_other_person_cannot_read_or_change_order():
    instance = book()
    order, _ = submit(instance)
    assert instance.list_for_speaker(PERSON_B) == ()
    with pytest.raises(DevelopmentOrderError):
        instance.get(order.order_id, verified_person_key=PERSON_B)
    with pytest.raises(DevelopmentOrderError):
        instance.cancel(order.order_id, verified_person_key=PERSON_B)
    with pytest.raises(DevelopmentOrderError):
        instance.list_for_speaker("c" * 64)
    with pytest.raises(DevelopmentOrderError):
        instance.start_execution(order.order_id, verified_person_key=PERSON_B)


def test_execution_is_blocked_until_plan_and_explicit_approval_step():
    instance = book()
    order, _ = submit(instance)
    assert order.stage is OrderStage.DRAFT
    with pytest.raises(DevelopmentOrderError):
        instance.start_execution(order.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError):
        instance.get(order.order_id, verified_person_key=PERSON_B)

    awaiting = ready_for_execution(instance, order.order_id)
    assert awaiting.stage is OrderStage.AWAITING_EXECUTION_APPROVAL
    assert awaiting.pending_approval is ApprovalAction.EXECUTE
    with pytest.raises(DevelopmentOrderError):
        instance.approve(order.order_id, ApprovalAction.DEPLOY, verified_person_key=PERSON_A)
    approved = instance.approve(
        order.order_id, ApprovalAction.EXECUTE, verified_person_key=PERSON_A
    )
    assert approved.stage is OrderStage.EXECUTION_APPROVED
    assert approved.approvals[0].person_key == PERSON_A
    assert instance.revoke_approval(
        order.order_id, verified_person_key=PERSON_A
    ).stage is OrderStage.AWAITING_EXECUTION_APPROVAL
    instance.approve(order.order_id, ApprovalAction.EXECUTE, verified_person_key=PERSON_A)


def test_tests_and_review_must_pass_before_push_is_available():
    instance = book()
    order, _ = submit(instance)
    with pytest.raises(DevelopmentOrderError):
        instance.approve(order.order_id, ApprovalAction.PUSH, verified_person_key=PERSON_A)

    ready_for_execution(instance, order.order_id)
    instance.approve(order.order_id, ApprovalAction.EXECUTE, verified_person_key=PERSON_A)
    instance.start_execution(order.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Execution started"):
        instance.cancel(order.order_id, verified_person_key=PERSON_A)
    instance.finish_execution(order.order_id, verified_person_key=PERSON_A, success=True, summary="Diff erstellt.")
    failed_tests = instance.record_tests(
        order.order_id, verified_person_key=PERSON_A, passed=False, summary="Ein Test fehlgeschlagen."
    )
    assert failed_tests.stage is OrderStage.FAILED
    assert failed_tests.error_code == "tests_failed"
    with pytest.raises(DevelopmentOrderError):
        instance.approve(order.order_id, ApprovalAction.PUSH, verified_person_key=PERSON_A)


def test_push_merge_and_deploy_each_need_distinct_approval():
    instance = book()
    order, _ = submit(instance)
    reviewed = ready_for_review(instance, order.order_id)
    assert reviewed.stage is OrderStage.LOCAL_REVIEWED

    with pytest.raises(DevelopmentOrderError):
        instance.mark_pushed(order.order_id, verified_person_key=PERSON_A)
    instance.approve(order.order_id, ApprovalAction.PUSH, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Execution started"):
        instance.cancel(order.order_id, verified_person_key=PERSON_A)
    assert instance.revoke_approval(order.order_id, verified_person_key=PERSON_A).stage is OrderStage.LOCAL_REVIEWED

    order, _ = submit(instance, request_id="remote-flow")
    ready_for_review(instance, order.order_id)
    instance.approve(order.order_id, ApprovalAction.PUSH, verified_person_key=PERSON_A)
    instance.start_push(order.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Action is active"):
        instance.cancel(order.order_id, verified_person_key=PERSON_A)
    pushed = instance.mark_pushed(order.order_id, verified_person_key=PERSON_A)
    assert pushed.pending_approval is ApprovalAction.MERGE
    with pytest.raises(DevelopmentOrderError, match="milestone"):
        instance.cancel(order.order_id, verified_person_key=PERSON_A)

    with pytest.raises(DevelopmentOrderError):
        instance.mark_merged(order.order_id, verified_person_key=PERSON_A)
    instance.approve(order.order_id, ApprovalAction.MERGE, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Execution started"):
        instance.cancel(order.order_id, verified_person_key=PERSON_A)
    assert instance.revoke_approval(order.order_id, verified_person_key=PERSON_A).stage is OrderStage.PUSHED
    instance.approve(order.order_id, ApprovalAction.MERGE, verified_person_key=PERSON_A)
    instance.start_merge(order.order_id, verified_person_key=PERSON_A)
    instance.mark_merged(order.order_id, verified_person_key=PERSON_A)

    deployment, _ = submit(instance, request_id="deploy-flow")
    ready_for_review(instance, deployment.order_id)
    instance.approve(deployment.order_id, ApprovalAction.PUSH, verified_person_key=PERSON_A)
    instance.start_push(deployment.order_id, verified_person_key=PERSON_A)
    instance.mark_pushed(deployment.order_id, verified_person_key=PERSON_A)
    instance.approve(deployment.order_id, ApprovalAction.MERGE, verified_person_key=PERSON_A)
    instance.start_merge(deployment.order_id, verified_person_key=PERSON_A)
    instance.mark_merged(deployment.order_id, verified_person_key=PERSON_A)
    instance.approve(deployment.order_id, ApprovalAction.DEPLOY, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Execution started"):
        instance.cancel(deployment.order_id, verified_person_key=PERSON_A)
    instance.revoke_approval(deployment.order_id, verified_person_key=PERSON_A)
    instance.approve(deployment.order_id, ApprovalAction.DEPLOY, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError):
        instance.mark_deployed(deployment.order_id, verified_person_key=PERSON_A)
    instance.start_deploy(deployment.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Action is active"):
        instance.cancel(deployment.order_id, verified_person_key=PERSON_A)
    assert instance.mark_deployed(deployment.order_id, verified_person_key=PERSON_A).stage is OrderStage.DEPLOYED


def test_cancel_is_explicit_and_terminal_orders_cannot_be_reopened():
    instance = book()
    order, _ = submit(instance)
    cancelled = instance.cancel(order.order_id, verified_person_key=PERSON_A)
    assert cancelled.stage is OrderStage.CANCELLED
    with pytest.raises(DevelopmentOrderError):
        instance.set_plan(order.order_id, verified_person_key=PERSON_A,
                          steps=(PlanStep("x", "y", "z"),))


def test_failed_execution_keeps_validated_diagnostic_and_failed_order_is_terminal():
    instance = book()
    order, _ = submit(instance)
    ready_for_execution(instance, order.order_id)
    instance.approve(order.order_id, ApprovalAction.EXECUTE, verified_person_key=PERSON_A)
    instance.start_execution(order.order_id, verified_person_key=PERSON_A)
    failed = instance.finish_execution(
        order.order_id, verified_person_key=PERSON_A, success=False,
        summary="Lokaler Schritt fehlgeschlagen.",
    )
    assert failed.execution_summary == "Lokaler Schritt fehlgeschlagen."
    with pytest.raises(DevelopmentOrderError):
        instance.cancel(order.order_id, verified_person_key=PERSON_A)
    other, _ = submit(instance, request_id="request-bad-summary")
    ready_for_execution(instance, other.order_id)
    instance.approve(other.order_id, ApprovalAction.EXECUTE, verified_person_key=PERSON_A)
    instance.start_execution(other.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Invalid execution result"):
        instance.finish_execution(
            other.order_id, verified_person_key=PERSON_A, success="yes", summary="no",
        )
    with pytest.raises(DevelopmentOrderError, match="Invalid execution summary"):
        instance.finish_execution(
            other.order_id, verified_person_key=PERSON_A, success=False, summary=123,
        )


@pytest.mark.parametrize("text", ["goal\u200b text", "goal\u202e text", "a\u2060b", "a\ufe0fb", "a\u180bb"])
def test_invisible_format_controls_except_joiners_are_rejected(text):
    instance = book()
    with pytest.raises(DevelopmentOrderError):
        submit(instance, goal=text)


def test_cancelled_and_deployed_request_ids_cannot_return_stuck_orders():
    instance = book()
    order, _ = submit(instance)
    instance.cancel(order.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="terminal order"):
        submit(instance)

    completed, _ = submit(instance, request_id="deployed-order")
    ready_for_review(instance, completed.order_id)
    for action, starter, marker in (
        (ApprovalAction.PUSH, instance.start_push, instance.mark_pushed),
        (ApprovalAction.MERGE, instance.start_merge, instance.mark_merged),
        (ApprovalAction.DEPLOY, instance.start_deploy, instance.mark_deployed),
    ):
        instance.approve(completed.order_id, action, verified_person_key=PERSON_A)
        starter(completed.order_id, verified_person_key=PERSON_A)
        marker(completed.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="terminal order"):
        submit(instance, request_id="deployed-order")


def test_unhashable_order_id_fails_with_safe_validation_error():
    instance = book()
    with pytest.raises(DevelopmentOrderError, match="Unknown development order"):
        instance.get([], verified_person_key=PERSON_A)


@pytest.mark.parametrize("text", ["goal\x0bline", "goal\x85line"])
def test_control_whitespace_is_rejected_before_normalization(text):
    instance = book()
    with pytest.raises(DevelopmentOrderError):
        submit(instance, goal=text)


def test_unicode_separator_whitespace_is_normalized_in_goal():
    order, _ = submit(goal="x\u00a0y")
    assert order.goal == "x y"


def test_canonically_equivalent_goal_retry_is_idempotent():
    instance = book()
    composed, created = submit(instance, goal="Ergänze den geprüften Ablauf.")
    decomposed, retry_created = submit(
        instance, goal=unicodedata.normalize("NFD", "Ergänze den geprüften Ablauf.")
    )
    assert created is True
    assert retry_created is False
    assert decomposed == composed


def test_repository_catalog_rejects_non_string_configuration_safely():
    with pytest.raises(DevelopmentOrderError):
        RepositoryCatalog(["lizard4king/smarthome-spotify-addon-ha"])


def test_oversized_raw_text_is_rejected_before_character_validation():
    with pytest.raises(DevelopmentOrderError):
        submit(goal="a" * 5000)


def test_order_capacity_is_bounded_per_speaker():
    instance = book()
    for index in range(100):
        submit(instance, request_id=f"capacity-{index}")
    other, created = submit(instance, request_id="capacity-other", person=PERSON_B)
    assert created is True
    assert other.submitter_person_key == PERSON_B
    with pytest.raises(DevelopmentOrderError, match="Speaker order capacity"):
        submit(instance, request_id="capacity-overflow")


def test_remote_failure_is_recorded_and_invalid_constructor_input_is_safe():
    instance = book()
    order, _ = submit(instance)
    ready_for_review(instance, order.order_id)
    instance.approve(order.order_id, ApprovalAction.PUSH, verified_person_key=PERSON_A)
    instance.start_push(order.order_id, verified_person_key=PERSON_A)
    with pytest.raises(DevelopmentOrderError, match="Invalid error code"):
        instance.record_remote_failure(
            order.order_id, verified_person_key=PERSON_A, error_code=None,
        )
    failed = instance.record_remote_failure(
        order.order_id, verified_person_key=PERSON_A, error_code="push_failed",
    )
    assert failed.stage is OrderStage.FAILED
    assert failed.error_code == "push_failed"
    with pytest.raises(DevelopmentOrderError):
        DevelopmentOrderBook(RepositoryCatalog("lizard4king/smarthome-spotify-addon-ha"), None)


@pytest.mark.parametrize("steps", [(), ("raw",), tuple(PlanStep("x", "y", "z") for _ in range(13))])
def test_plan_requires_bounded_typed_steps(steps):
    instance = book()
    order, _ = submit(instance)
    with pytest.raises(DevelopmentOrderError):
        instance.set_plan(order.order_id, verified_person_key=PERSON_A, steps=steps)


def test_no_default_speaker_or_authorized_person_configuration():
    with pytest.raises(DevelopmentOrderError, match="No authorized"):
        DevelopmentOrderBook(RepositoryCatalog("lizard4king/smarthome-spotify-addon-ha"), set())
