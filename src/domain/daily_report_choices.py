"""Daily-report choices carried with the operating database and its backups."""

from dataclasses import dataclass
from uuid import UUID

from .revisions import AuditChange, ChangeOperation, RevisionMetadata, RevisionRecord, RevisionSource
from .roster import Meal

CHOICES_KIND = "daily_report_choices"
CHOICES_ID = UUID("9691458a-37b9-4ccc-a918-82156d90f0f6")


@dataclass(frozen=True, slots=True)
class DailyReportChoices:
    meals: tuple[Meal, ...] = tuple(Meal)
    excluded_forms: tuple[str | None, ...] = ()

    def __post_init__(self):
        if not self.meals or any(not isinstance(meal, Meal) for meal in self.meals):
            raise ValueError("일일식수의 식종을 하나 이상 선택해주세요.")
        if any(form is not None and (not isinstance(form, str) or not form or form != " ".join(form.split()))
               for form in self.excluded_forms):
            raise ValueError("일일식수의 제외 배송 형태가 올바르지 않습니다.")
        object.__setattr__(self, "meals", tuple(meal for meal in Meal if meal in self.meals))
        object.__setattr__(self, "excluded_forms", tuple(sorted(set(self.excluded_forms), key=lambda form: (form is not None, form or ""))))


def daily_report_choices_from_snapshot(snapshot):
    state = snapshot.entity(CHOICES_KIND, CHOICES_ID)
    if state is None:
        return DailyReportChoices()
    return DailyReportChoices(tuple(Meal(value) for value in state["meals"]), tuple(state["excluded_forms"]))


def build_daily_report_choices_revision(snapshot, choices, *, actor, recorded_at, revision_id):
    if not isinstance(choices, DailyReportChoices):
        raise TypeError("일일식수 선택 설정이 올바르지 않습니다.")
    if daily_report_choices_from_snapshot(snapshot) == choices:
        return None
    before = snapshot.entity(CHOICES_KIND, CHOICES_ID)
    after = {"meals": [meal.value for meal in choices.meals], "excluded_forms": list(choices.excluded_forms)}
    return RevisionRecord(
        RevisionMetadata(revision_id, snapshot.revision_id, recorded_at, None, actor, "일일식수 식종·배송 형태 선택", RevisionSource.UI),
        (AuditChange(CHOICES_KIND, CHOICES_ID, ChangeOperation.INSERT if before is None else ChangeOperation.UPDATE, before, after),),
    )
