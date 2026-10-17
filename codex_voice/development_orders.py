"""Offline prototype for issue-bound, explicitly approved development orders.

This module is a data model and state machine only. It has no GitHub, shell,
SSH, model, filesystem, or deployment client.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import re
import threading
import unicodedata
from uuid import uuid4


SMART_HOME_REPOSITORY = "lizard4king/SmartHome"
ADDON_REPOSITORY_CANDIDATES = frozenset({
    "lizard4king/smarthome-spotify-addon-ha",
})
_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class DevelopmentOrderError(ValueError):
    """Safe validation or transition failure for the local prototype."""


class Project(StrEnum):
    SMART_HOME = "smarthome"
    HA_ADDON = "ha_addon"


class OrderStage(StrEnum):
    DRAFT = "draft"
    AWAITING_EXECUTION_APPROVAL = "awaiting_execution_approval"
    EXECUTION_APPROVED = "execution_approved"
    EXECUTING = "executing"
    AWAITING_TESTS = "awaiting_tests"
    AWAITING_REVIEW = "awaiting_review"
    LOCAL_REVIEWED = "local_reviewed"
    PUSH_APPROVED = "push_approved"
    PUSHING = "pushing"
    PUSHED = "pushed"
    MERGE_APPROVED = "merge_approved"
    MERGING = "merging"
    MERGED = "merged"
    DEPLOY_APPROVED = "deploy_approved"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLOSED = "closed"


TERMINAL_STAGES = frozenset({OrderStage.DEPLOYED, OrderStage.FAILED, OrderStage.CANCELLED, OrderStage.CLOSED})
MAX_ORDERS = 1000
MAX_ORDERS_PER_SPEAKER = 100


class ApprovalAction(StrEnum):
    EXECUTE = "execute"
    PUSH = "push"
    MERGE = "merge"
    DEPLOY = "deploy"


@dataclass(frozen=True, slots=True)
class RepositoryCatalog:
    """The confirmed HA add-on target must still be selected explicitly."""

    addon_repository: str

    def __post_init__(self) -> None:
        if (not isinstance(self.addon_repository, str)
                or self.addon_repository not in ADDON_REPOSITORY_CANDIDATES):
            raise DevelopmentOrderError("Add-on repository is not allowlisted")

    def resolve(self, project: Project) -> str:
        if project is Project.SMART_HOME:
            return SMART_HOME_REPOSITORY
        if project is Project.HA_ADDON:
            return self.addon_repository
        raise DevelopmentOrderError("Unknown project")


@dataclass(frozen=True, slots=True)
class IssueReference:
    repository: str
    number: int

    def __post_init__(self) -> None:
        if self.repository != SMART_HOME_REPOSITORY:
            raise DevelopmentOrderError("Issue must belong to the SmartHome control repository")
        if type(self.number) is not int or not 1 <= self.number <= 9_999_999:
            raise DevelopmentOrderError("Invalid issue number")

    @property
    def display(self) -> str:
        return f"{self.repository}#{self.number}"


@dataclass(frozen=True, slots=True)
class PlanStep:
    title: str
    detail: str
    verification: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _bounded_text(self.title, 120, "plan title"))
        object.__setattr__(self, "detail", _bounded_text(self.detail, 600, "plan detail"))
        object.__setattr__(
            self, "verification", _bounded_text(self.verification, 240, "verification")
        )


@dataclass(frozen=True, slots=True)
class Approval:
    action: ApprovalAction
    person_key: str


@dataclass(frozen=True, slots=True)
class DevelopmentOrder:
    order_id: str
    request_id: str
    issue: IssueReference
    project: Project
    repository: str
    goal: str
    submitter_person_key: str
    stage: OrderStage = OrderStage.DRAFT
    plan: tuple[PlanStep, ...] = ()
    approvals: tuple[Approval, ...] = ()
    execution_summary: str = ""
    test_summary: str = ""
    review_summary: str = ""
    error_code: str = ""
    closed_after_stage: OrderStage | None = None

    @property
    def branch_name(self) -> str:
        return f"codex/{self.order_id}"

    @property
    def pending_approval(self) -> ApprovalAction | None:
        return {
            OrderStage.AWAITING_EXECUTION_APPROVAL: ApprovalAction.EXECUTE,
            OrderStage.LOCAL_REVIEWED: ApprovalAction.PUSH,
            OrderStage.PUSHED: ApprovalAction.MERGE,
            OrderStage.MERGED: ApprovalAction.DEPLOY,
        }.get(self.stage)


def _bounded_text(value: object, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise DevelopmentOrderError(f"Invalid {label}")
    if len(value) > maximum * 4:
        raise DevelopmentOrderError(f"Invalid {label}")
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character) == "Cc" and character not in "\t\r\n"
           for character in value):
        raise DevelopmentOrderError(f"Invalid {label}")
    normalized = " ".join(value.split())
    if (not normalized or len(normalized) > maximum
            or any((unicodedata.category(character) in {"Cs", "Cn"}
                    or (unicodedata.category(character) == "Cf"
                        and character not in {"\u200c", "\u200d"})
                    or (unicodedata.category(character) == "Mn"
                        and (0x180B <= ord(character) <= 0x180F
                             or 0xFE00 <= ord(character) <= 0xFE0F
                             or 0xE0100 <= ord(character) <= 0xE01EF)))
                   for character in normalized)):
        raise DevelopmentOrderError(f"Invalid {label}")
    return normalized


def _person_key(value: object) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise DevelopmentOrderError("A verified speaker key is required")
    return value


class DevelopmentOrderBook:
    """Thread-safe, process-local order records; no persistence or side effects."""

    def __init__(self, catalog: RepositoryCatalog, authorized_person_keys: set[str] | frozenset[str]):
        if not isinstance(catalog, RepositoryCatalog):
            raise DevelopmentOrderError("A validated repository catalog is required")
        try:
            keys = frozenset(_person_key(key) for key in authorized_person_keys)
        except TypeError as exc:
            raise DevelopmentOrderError("No authorized speaker keys configured") from exc
        if not keys:
            raise DevelopmentOrderError("No authorized speaker keys configured")
        self._catalog = catalog
        self._authorized_person_keys = keys
        self._orders: dict[str, DevelopmentOrder] = {}
        self._request_index: dict[tuple[str, str], tuple[str, str]] = {}
        self._lock = threading.RLock()

    def _authorized(self, person_key: str) -> str:
        key = _person_key(person_key)
        if key not in self._authorized_person_keys:
            raise DevelopmentOrderError("Speaker is not authorized for development orders")
        return key

    def submit(
        self,
        *,
        request_id: str,
        issue: IssueReference,
        project: Project,
        goal: str,
        verified_person_key: str,
    ) -> tuple[DevelopmentOrder, bool]:
        person = self._authorized(verified_person_key)
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            raise DevelopmentOrderError("Invalid request identifier")
        if not isinstance(issue, IssueReference) or not isinstance(project, Project):
            raise DevelopmentOrderError("Issue and repository selection are required")
        repository = self._catalog.resolve(project)
        normalized_goal = _bounded_text(goal, 1200, "development goal")
        digest = hashlib.sha256(json.dumps(
            [issue.repository, issue.number, project.value, repository, normalized_goal],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        key = (person, request_id)
        with self._lock:
            existing = self._request_index.get(key)
            if existing is not None:
                old_order_id, old_digest = existing
                if old_digest != digest:
                    raise DevelopmentOrderError("Duplicate request identifier has different content")
                old_order = self._orders[old_order_id]
                if old_order.stage in TERMINAL_STAGES:
                    raise DevelopmentOrderError("Request identifier belongs to a terminal order")
                return old_order, False
            if len(self._orders) >= MAX_ORDERS:
                raise DevelopmentOrderError("Development order capacity reached")
            if sum(order.submitter_person_key == person for order in self._orders.values()) >= MAX_ORDERS_PER_SPEAKER:
                raise DevelopmentOrderError("Speaker order capacity reached")
            order = DevelopmentOrder(
                order_id=uuid4().hex,
                request_id=request_id,
                issue=issue,
                project=project,
                repository=repository,
                goal=normalized_goal,
                submitter_person_key=person,
            )
            self._orders[order.order_id] = order
            self._request_index[key] = (order.order_id, digest)
            return order, True

    def get(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        return self._owned_order(order_id, person)

    def _get(self, order_id: str) -> DevelopmentOrder:
        if not isinstance(order_id, str):
            raise DevelopmentOrderError("Unknown development order")
        with self._lock:
            try:
                return self._orders[order_id]
            except KeyError as exc:
                raise DevelopmentOrderError("Unknown development order") from exc

    def list_for_speaker(self, verified_person_key: str) -> tuple[DevelopmentOrder, ...]:
        person = self._authorized(verified_person_key)
        with self._lock:
            return tuple(order for order in self._orders.values()
                         if order.submitter_person_key == person)

    def set_plan(
        self, order_id: str, *, verified_person_key: str, steps: tuple[PlanStep, ...]
    ) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        if order.stage is not OrderStage.DRAFT:
            raise DevelopmentOrderError("Plan can only be set on a draft order")
        if (not isinstance(steps, tuple) or not 1 <= len(steps) <= 12
                or any(not isinstance(step, PlanStep) for step in steps)):
            raise DevelopmentOrderError("Plan must contain 1 to 12 validated steps")
        return self._replace(order, stage=OrderStage.AWAITING_EXECUTION_APPROVAL, plan=steps)

    def approve(
        self, order_id: str, action: ApprovalAction, *, verified_person_key: str
    ) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        if not isinstance(action, ApprovalAction):
            raise DevelopmentOrderError("Invalid approval action")
        order = self._owned_order(order_id, person)
        transitions = {
            (OrderStage.AWAITING_EXECUTION_APPROVAL, ApprovalAction.EXECUTE): OrderStage.EXECUTION_APPROVED,
            (OrderStage.LOCAL_REVIEWED, ApprovalAction.PUSH): OrderStage.PUSH_APPROVED,
            (OrderStage.PUSHED, ApprovalAction.MERGE): OrderStage.MERGE_APPROVED,
            (OrderStage.MERGED, ApprovalAction.DEPLOY): OrderStage.DEPLOY_APPROVED,
        }
        next_stage = transitions.get((order.stage, action))
        if next_stage is None:
            raise DevelopmentOrderError("Approval is not valid at this stage")
        approval = Approval(action=action, person_key=person)
        return self._replace(order, stage=next_stage, approvals=order.approvals + (approval,))

    def start_execution(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        if order.stage is not OrderStage.EXECUTION_APPROVED:
            raise DevelopmentOrderError("Execution requires explicit approval")
        return self._replace(order, stage=OrderStage.EXECUTING)

    def finish_execution(self, order_id: str, *, verified_person_key: str, success: bool, summary: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        if order.stage is not OrderStage.EXECUTING:
            raise DevelopmentOrderError("No execution is active")
        if type(success) is not bool:
            raise DevelopmentOrderError("Invalid execution result")
        text = _bounded_text(summary, 1000, "execution summary")
        return self._replace(
            order,
            stage=OrderStage.AWAITING_TESTS if success else OrderStage.FAILED,
            execution_summary=text,
            error_code="" if success else "execution_failed",
        )

    def record_tests(self, order_id: str, *, verified_person_key: str, passed: bool, summary: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        if order.stage is not OrderStage.AWAITING_TESTS:
            raise DevelopmentOrderError("Tests are not expected at this stage")
        if type(passed) is not bool:
            raise DevelopmentOrderError("Invalid test result")
        text = _bounded_text(summary, 1000, "test summary")
        return self._replace(
            order,
            stage=OrderStage.AWAITING_REVIEW if passed else OrderStage.FAILED,
            test_summary=text,
            error_code="" if passed else "tests_failed",
        )

    def record_review(self, order_id: str, *, verified_person_key: str, passed: bool, summary: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        if order.stage is not OrderStage.AWAITING_REVIEW:
            raise DevelopmentOrderError("Review is not expected at this stage")
        if type(passed) is not bool:
            raise DevelopmentOrderError("Invalid review result")
        text = _bounded_text(summary, 1000, "review summary")
        return self._replace(
            order,
            stage=OrderStage.LOCAL_REVIEWED if passed else OrderStage.FAILED,
            review_summary=text,
            error_code="" if passed else "review_failed",
        )

    def start_push(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        return self._advance(order_id, OrderStage.PUSH_APPROVED, OrderStage.PUSHING, verified_person_key)

    def mark_pushed(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        return self._advance(order_id, OrderStage.PUSHING, OrderStage.PUSHED, verified_person_key)

    def start_merge(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        return self._advance(order_id, OrderStage.MERGE_APPROVED, OrderStage.MERGING, verified_person_key)

    def mark_merged(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        return self._advance(order_id, OrderStage.MERGING, OrderStage.MERGED, verified_person_key)

    def start_deploy(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        return self._advance(order_id, OrderStage.DEPLOY_APPROVED, OrderStage.DEPLOYING, verified_person_key)

    def mark_deployed(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        return self._advance(order_id, OrderStage.DEPLOYING, OrderStage.DEPLOYED, verified_person_key)

    def record_remote_failure(self, order_id: str, *, verified_person_key: str,
                              error_code: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        if order.stage not in {OrderStage.PUSHING, OrderStage.MERGING, OrderStage.DEPLOYING}:
            raise DevelopmentOrderError("No remote action is active")
        if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
            raise DevelopmentOrderError("Invalid error code")
        return self._replace(order, stage=OrderStage.FAILED, error_code=error_code)

    def revoke_approval(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        rollback = {
            OrderStage.EXECUTION_APPROVED: (
                OrderStage.AWAITING_EXECUTION_APPROVAL, ApprovalAction.EXECUTE
            ),
            OrderStage.PUSH_APPROVED: (OrderStage.LOCAL_REVIEWED, ApprovalAction.PUSH),
            OrderStage.MERGE_APPROVED: (OrderStage.PUSHED, ApprovalAction.MERGE),
            OrderStage.DEPLOY_APPROVED: (OrderStage.MERGED, ApprovalAction.DEPLOY),
        }.get(order.stage)
        if rollback is None or not order.approvals or order.approvals[-1].action is not rollback[1]:
            raise DevelopmentOrderError("No revocable approval is pending")
        return self._replace(order, stage=rollback[0], approvals=order.approvals[:-1])

    def cancel(self, order_id: str, *, verified_person_key: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        cancellable = {
            OrderStage.DRAFT, OrderStage.AWAITING_EXECUTION_APPROVAL,
            OrderStage.EXECUTION_APPROVED,
        }
        if order.stage not in cancellable:
            if order.stage in TERMINAL_STAGES:
                raise DevelopmentOrderError("Order is already terminal")
            if order.stage in {OrderStage.PUSHING, OrderStage.MERGING, OrderStage.DEPLOYING}:
                raise DevelopmentOrderError("Action is active; cancellation is unavailable")
            raise DevelopmentOrderError("Execution started or remote milestone reached; cancellation is unavailable")
        return self._replace(order, stage=OrderStage.CANCELLED)

    def close_without_further_action(
        self, order_id: str, *, verified_person_key: str
    ) -> DevelopmentOrder:
        """End an idle order while retaining its completed work and milestones."""
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        closable = {
            OrderStage.AWAITING_TESTS, OrderStage.AWAITING_REVIEW,
            OrderStage.LOCAL_REVIEWED, OrderStage.PUSH_APPROVED,
            OrderStage.PUSHED, OrderStage.MERGE_APPROVED,
            OrderStage.MERGED, OrderStage.DEPLOY_APPROVED,
        }
        if order.stage not in closable:
            raise DevelopmentOrderError("Only an idle, nonterminal result can be closed")
        return self._replace(order, stage=OrderStage.CLOSED, closed_after_stage=order.stage)

    def _owned_order(self, order_id: str, person: str) -> DevelopmentOrder:
        order = self._get(order_id)
        if order.submitter_person_key != person:
            raise DevelopmentOrderError("Unknown development order")
        return order

    def _advance(self, order_id: str, current: OrderStage, next_stage: OrderStage,
                 verified_person_key: str) -> DevelopmentOrder:
        person = self._authorized(verified_person_key)
        order = self._owned_order(order_id, person)
        if order.stage is not current:
            raise DevelopmentOrderError("Action requires its separate approval")
        return self._replace(order, stage=next_stage)

    def _replace(self, order: DevelopmentOrder, **changes) -> DevelopmentOrder:
        error_code = changes.get("error_code", order.error_code)
        if not isinstance(error_code, str) or (error_code and not _ERROR_CODE_RE.fullmatch(error_code)):
            raise DevelopmentOrderError("Invalid error code")
        updated = replace(order, **changes)
        with self._lock:
            if self._orders.get(order.order_id) != order:
                raise DevelopmentOrderError("Order changed concurrently")
            self._orders[order.order_id] = updated
            return updated
