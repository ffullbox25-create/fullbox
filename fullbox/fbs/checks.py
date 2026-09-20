from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.core.checks import Error, Tags, register


@dataclass(frozen=True)
class UnsafeLockJoin:
    line: int
    model_name: str
    relation_path: str
    nullable_segment: str


def _outer_call_chain(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    current = node
    while True:
        parent = parents.get(current)
        if isinstance(parent, ast.Attribute) and parent.value is current:
            current = parent
            continue
        if isinstance(parent, ast.Call) and parent.func is current:
            current = parent
            continue
        return current


def _call_chain(node: ast.AST) -> tuple[list[tuple[str, ast.Call]], ast.AST]:
    calls: list[tuple[str, ast.Call]] = []
    current = node
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        calls.append((current.func.attr, current))
        current = current.func.value
    return calls, current


def _root_model_name(node: ast.AST) -> str | None:
    current = node
    while isinstance(current, ast.Attribute):
        if current.attr == "objects" and isinstance(current.value, ast.Name):
            return current.value.id
        current = current.value
    return None


def _model_index() -> dict[str, list[type]]:
    result: dict[str, list[type]] = {}
    for model in apps.get_models():
        result.setdefault(model.__name__, []).append(model)
    return result


def _nullable_relation_segment(model, relation_path: str) -> str | None:
    current_model = model
    for part in relation_path.split("__"):
        try:
            field = current_model._meta.get_field(part)
        except Exception:
            return None
        is_reverse_relation = bool(
            getattr(field, "auto_created", False)
            and not getattr(field, "concrete", False)
        )
        if is_reverse_relation or bool(getattr(field, "null", False)):
            return f"{current_model.__name__}.{part}"
        related_model = getattr(field, "related_model", None)
        if related_model is None:
            return None
        current_model = related_model
    return None


def find_unsafe_lock_joins(
    source: str,
    *,
    models_by_name: dict[str, list[type]] | None = None,
) -> list[UnsafeLockJoin]:
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    models_by_name = models_by_name or _model_index()
    issues: list[UnsafeLockJoin] = []

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "select_for_update"
        ):
            continue
        if any(keyword.arg == "of" for keyword in node.keywords):
            continue

        outer = _outer_call_chain(node, parents)
        calls, base = _call_chain(outer)
        select_related_calls = [
            call for method_name, call in calls if method_name == "select_related"
        ]
        if not select_related_calls:
            continue

        model_name = _root_model_name(base)
        matching_models = models_by_name.get(model_name or "", [])
        if len(matching_models) != 1:
            continue
        model = matching_models[0]

        for related_call in select_related_calls:
            for argument in related_call.args:
                if not (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                ):
                    issues.append(
                        UnsafeLockJoin(
                            line=node.lineno,
                            model_name=model_name or model.__name__,
                            relation_path="<dynamic>",
                            nullable_segment="невозможно доказать безопасность",
                        )
                    )
                    continue
                nullable_segment = _nullable_relation_segment(model, argument.value)
                if nullable_segment:
                    issues.append(
                        UnsafeLockJoin(
                            line=node.lineno,
                            model_name=model_name or model.__name__,
                            relation_path=argument.value,
                            nullable_segment=nullable_segment,
                        )
                    )
    return issues


@register(Tags.database)
def check_fbs_select_for_update_scope(app_configs, **kwargs):
    service_root = Path(__file__).resolve().parent / "services"
    errors = []
    for path in sorted(service_root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for issue in find_unsafe_lock_joins(source):
            relative_path = path.relative_to(service_root.parent)
            errors.append(
                Error(
                    (
                        "FOR UPDATE совмещен с nullable select_related без "
                        f"явной области блокировки: {relative_path}:{issue.line}, "
                        f"{issue.relation_path} ({issue.nullable_segment})."
                    ),
                    hint=(
                        'Укажите select_for_update(of=("self",)) либо явно '
                        "перечислите таблицы, которые действительно нужно блокировать."
                    ),
                    obj=f"{relative_path}:{issue.line}",
                    id="fbs.E001",
                )
            )
    return errors
