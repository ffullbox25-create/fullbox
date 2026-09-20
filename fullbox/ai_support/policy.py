"""Safety policy for actions proposed by the programming agent.

The communication module records what the agent wants to do, but this policy
is the final gate before an action may move into execution.  The agent never
gets a generic production shell or unrestricted database write path.
"""

from dataclasses import dataclass


POLICY_AUTOMATIC = "automatic"
POLICY_APPROVAL = "approval"
POLICY_PROGRAMMER = "programmer"


AUTOMATIC_ACTIONS = frozenset(
    {
        "read_logs",
        "read_database",
        "inspect_services",
        "inspect_request",
        "reproduce_in_sandbox",
        "prepare_patch",
        "run_tests",
    }
)

APPROVAL_ACTIONS = frozenset(
    {
        "deploy_patch",
        "restart_service",
        "retry_idempotent_operation",
        "correct_single_request_status",
    }
)

PROGRAMMER_ONLY_ACTIONS = frozenset(
    {
        "change_stock",
        "change_movement",
        "bulk_data_change",
        "schema_change",
        "permission_change",
        "new_feature",
        "architecture_change",
    }
)


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    level: str
    automatic_allowed: bool
    human_approval_required: bool
    programmer_required: bool
    explanation: str


def policy_for_action(action_type: str) -> ActionPolicy:
    normalized = str(action_type or "").strip()
    if normalized in AUTOMATIC_ACTIONS:
        return ActionPolicy(
            level=POLICY_AUTOMATIC,
            automatic_allowed=True,
            human_approval_required=False,
            programmer_required=False,
            explanation="Разрешено только чтение, диагностика или подготовка патча вне production.",
        )
    if normalized in APPROVAL_ACTIONS:
        return ActionPolicy(
            level=POLICY_APPROVAL,
            automatic_allowed=False,
            human_approval_required=True,
            programmer_required=False,
            explanation="Нужно подтверждение ответственного сотрудника перед выполнением.",
        )
    return ActionPolicy(
        level=POLICY_PROGRAMMER,
        automatic_allowed=False,
        human_approval_required=True,
        programmer_required=True,
        explanation=(
            "Действие затрагивает защищённую область и должно быть передано программисту. "
            "ИИ-агент не может его выполнять."
        ),
    )
