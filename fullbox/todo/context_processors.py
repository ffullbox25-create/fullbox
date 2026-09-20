from employees.access import get_request_employee

from .attention import unread_task_count


def task_attention(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"todo_unread_count": 0}
    employee = get_request_employee(request)
    return {"todo_unread_count": unread_task_count(employee)}
