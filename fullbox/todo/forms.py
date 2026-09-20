from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from employees.models import Employee

from .models import Task, TaskChecklistItem, TaskComment, default_due_date


WAREHOUSE_TASK_ROLES = {
    "storekeeper",
    "picker",
    "reachtruck_driver",
    "super_car",
    "processing_head",
    "processing_worker",
    "packer",
}


class TaskForm(forms.ModelForm):
    due_date = forms.DateTimeField(
        required=True,
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Дедлайн",
    )
    assigned_to = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        label="Исполнитель",
        empty_label="Не назначен",
    )
    observer = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        label="Наблюдатель",
        empty_label="Не назначен",
    )
    participants = forms.ModelMultipleChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        label="Соисполнители",
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Task
        fields = [
            "title",
            "description",
            "assigned_to",
            "participants",
            "observer",
            "priority",
            "due_date",
        ]

    def __init__(self, *args, **kwargs):
        self.task_kind = kwargs.pop("task_kind", None) or getattr(
            kwargs.get("instance"), "kind", Task.KIND_SYSTEM
        )
        self.request_employee = kwargs.pop("request_employee", None)
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            if self.instance and self.instance.pk and self.instance.due_date:
                self.initial["due_date"] = timezone.localtime(self.instance.due_date)
            else:
                self.initial["due_date"] = timezone.localtime(default_due_date())
        active_employees = Employee.objects.filter(is_active=True)
        if self.task_kind == Task.KIND_WAREHOUSE_INTERNAL:
            active_employees = active_employees.filter(
                role__in=WAREHOUSE_TASK_ROLES,
                user__isnull=False,
            )
            self.fields["assigned_to"].required = True
            self.fields["assigned_to"].empty_label = "Выберите ответственного"
        assignment_extra_ids = []
        if self.instance and self.instance.assigned_to_id:
            assignment_extra_ids.append(self.instance.assigned_to_id)
        if self.instance and self.instance.pk:
            assignment_extra_ids.extend(
                self.instance.participants.values_list("id", flat=True)
            )
        if assignment_extra_ids:
            active_employees = active_employees | Employee.objects.filter(
                id__in=assignment_extra_ids
            )
        self.fields["assigned_to"].queryset = active_employees.distinct()
        self.fields["participants"].queryset = active_employees.distinct()
        self.fields["observer"].queryset = Employee.objects.filter(is_active=True)
        if self.instance and self.instance.observer_id:
            self.fields["observer"].queryset = (
                self.fields["observer"].queryset
                | Employee.objects.filter(id=self.instance.observer_id)
            ).distinct()
        if (
            self.task_kind == Task.KIND_WAREHOUSE_INTERNAL
            and not self.is_bound
            and not self.instance.pk
            and self.request_employee
        ):
            self.initial["observer"] = self.request_employee.pk

    def clean(self):
        cleaned_data = super().clean()
        if self.task_kind != Task.KIND_WAREHOUSE_INTERNAL:
            return cleaned_data
        assigned_to = cleaned_data.get("assigned_to")
        participants = cleaned_data.get("participants")
        if assigned_to and participants and assigned_to in participants:
            self.add_error(
                "participants",
                "Основной ответственный не должен дублироваться в соисполнителях.",
            )
        return cleaned_data


class TaskChecklistItemForm(forms.ModelForm):
    class Meta:
        model = TaskChecklistItem
        fields = ["title"]
        widgets = {
            "title": forms.TextInput(
                attrs={"placeholder": "Например: проверить маркировку"}
            )
        }
        labels = {"title": "Пункт"}


class BaseTaskChecklistFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        titles = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue
            title = str(form.cleaned_data.get("title") or "").strip()
            if not title:
                continue
            normalized = title.casefold()
            if normalized in titles:
                form.add_error("title", "Такой пункт уже есть в чек-листе.")
            titles.add(normalized)


TaskChecklistFormSet = inlineformset_factory(
    Task,
    TaskChecklistItem,
    form=TaskChecklistItemForm,
    formset=BaseTaskChecklistFormSet,
    extra=1,
    can_delete=True,
)


class TaskCommentForm(forms.ModelForm):
    class Meta:
        model = TaskComment
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Добавьте комментарий",
                }
            )
        }
        labels = {"body": "Комментарий"}


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class TaskAttachmentForm(forms.Form):
    files = forms.FileField(
        label="Файлы",
        required=False,
        widget=MultiFileInput(attrs={"multiple": True}),
    )
