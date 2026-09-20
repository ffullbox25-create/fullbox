from __future__ import annotations


WORKSPACE_SHELL_ROLES = frozenset(
    {
        "storekeeper",
        "head_manager",
        "director",
        "admin",
        "developer",
    }
)


def workspace_shell_enabled(role: str | None) -> bool:
    return role in WORKSPACE_SHELL_ROLES
