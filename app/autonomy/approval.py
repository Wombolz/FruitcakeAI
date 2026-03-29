"""
FruitcakeAI v5 — Approval primitives (Phase 4)

Shared module imported by both the task runner (to arm the gate) and the
tool dispatcher (to check it). Using a ContextVar instead of a global means
concurrent tasks never interfere with each other's approval state.
"""

from contextvars import ContextVar


class ApprovalRequired(Exception):
    """
    Raised by _call_tool() when a task-mode agent tries to invoke an
    approval-gated tool without pre-approval.

    The TaskRunner catches this, sets task.status = "waiting_approval",
    and returns without completing the task. The user approves via
    PATCH /tasks/{id} {"approved": true}, which re-queues the task with
    pre_approved=True so it can proceed.
    """
    pass


# Tools that must pause for user approval before executing.
# Phase 5 will add "send_email" when the email MCP server is wired.
APPROVAL_REQUIRED_TOOLS: frozenset[str] = frozenset({
    "create_event",
    "delete_event",
})

# ContextVar: set to True by TaskRunner before calling run_agent() when
# task.requires_approval=True and the task has not been pre-approved.
# Automatically scoped to the current async task — no cross-task leakage.
_approval_armed: ContextVar[bool] = ContextVar("_approval_armed", default=False)
