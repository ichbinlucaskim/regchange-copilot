"""출력 내부 정합성 검사 — **LLM 을 부르기 전에, 코드로만** (기준선 §11 의 「대안 1번」).

목적:
    영향평가 초안 하나를 그 자신의 입력과 대조해, **거짓일 수밖에 없는** 항목을 폐기한다.
    gate 3단(모델 호출)에 도달하는 항목 수를 줄이는 것이 부수 목적이다.

구현 이유:
    기준선 문서(`docs/11-obligation-extraction-baseline.md` §11)는 gate 3단 앞에 모순
    필터를 두는 것을 「싸고 확실한 대안 1번」으로 예고했다. 근거는 case-013 에서 관측된
    조합이었다 — `suggested_action=NEW_PROVISION_REVIEW`("담을 조항이 없다")와
    `citations=2`("근거가 있다")가 동시에 참일 수 없다는 것.

    **같은 문서가 그 규칙을 만들기 전에 전수 관측을 요구했고(「한 건에 과적합하지 마라」),
    전수 관측이 그 규칙을 기각했다.** 골든셋 15건 + Opus 대조 4건의 원본 출력 21건을
    전부 훑은 결과는 `docs/12-impact-assessment-results.md` §3 에 있고 요약은 아래다.

      | 후보 규칙 | 발화 | 그중 **정답이었던** 출력 | 판정 |
      |---|---|---|---|
      | `action=NEW_PROVISION_REVIEW` ∧ 인용>0 | 12/21 | 10 | **기각** |
      | 〃 ∧ **모든** 의무에 인용이 붙음 | 5/21 | 3 | **기각** |
      | `obligation_type=EDITORIAL` ∧ 인용>0 | 2/21 | 1 | **기각** |
      | `status=INSUFFICIENT_EVIDENCE` ∧ 인용>0 | 0/21 | — | 관측 없음 |
      | `status=OK` ∧ 인용=0 | 0/21 | — | 관측 없음 |
      | `action=TRANSFER_AND_CLOSE` ∧ 인용>0 | 0/21 | — | 관측 없음 |
      | 의무=0 ∧ `action=NEW_PROVISION_REVIEW` | 0/21 | — | 관측 없음 |

    첫 세 줄은 **모순이 아니라 정상 출력이었다.** "일부 의무는 기존 조항에 걸리고 일부는
    담을 조항이 없다"가 한 개정에서 동시에 참일 수 있으며, 실제로 그것이 다수였다.
    나머지 줄은 한 번도 관측되지 않았으므로 만들지 않는다 — 4단계 지시가 "관측되지 않은
    조합을 상상해서 넣지 마라"고 한 자리다.

    **그래서 이 모듈이 강제하는 규칙은 종류가 다르다.** 두 부류를 가른다.

      | 부류 | 강제하는가 | 왜 |
      |---|---|---|
      | **참일 수 없는 것** (참조 무결성) | 강제한다 | 모델의 판단력과 무관하다 |
      | **참일 수도 있는 조합** (판단의 모순) | 관측이 허락할 때만 | 위 표가 보여준 것이 이것이다 |

    첫 부류는 자기 입력에 없는 것을 가리키는 출력이며 어떤 경우에도 옳지 않다.
    둘째 부류는 그럴듯한 모순이 실제로는 정상 출력이었던 경우다.

    `obligation_index` 범위 검사가 첫 부류다. 초안은 자기가 받은 의무 목록의 순번을
    가리키며, 목록 밖을 가리키면 **그 영향 문단이 왜 걸렸는지에 대한 연결이 끊긴 것**이다.
    끊긴 연결을 그대로 두면 검토 화면에서 "이 조항이 어느 의무 때문에 걸렸는가"에 답할 수
    없고, 담당자는 답이 없다는 사실조차 모른다.

    **기대했던 절감은 일어나지 않는다.** 이 필터는 evaluator 호출을 거의 줄이지 않는다.
    그 사실을 여기 적는 이유는, 기준선이 예고한 「싸고 확실한 1번」이 **관측 뒤에 사라졌기
    때문**이다. 남은 것은 2번(gate 3단)이며 그것은 호출을 쓴다.

트레이드오프:
    - 규칙이 하나뿐인 모듈이다. 그럼에도 파일을 두는 이유는 **관측 결과를 코드가 들고
      있어야 하기 때문**이다. 위 표가 문서에만 있으면 다음 사람이 같은 규칙을 다시
      제안하고 다시 기각하는 데 같은 측정을 반복한다. `EVALUATED_RULES` 가 그 기록이다.
    - 폐기 단위가 **영향 문단 하나**다. 초안 전체를 버리지 않는다 — 연결이 끊긴 항목
      하나 때문에 근거가 실재하는 나머지를 버리면, 그 폐기가 "영향 없음"으로 보인다.
    - 판정 결과를 예외가 아니라 값으로 돌려준다. 예외로 만들면 호출부가 잡아서 무엇을
      폐기했는지 다시 조립해야 하고, 조립하는 과정에서 사유가 사라진다.

엣지 케이스:
    - **의무 목록이 0건인데 영향 문단이 있음**: 모든 `obligation_index` 가 범위 밖이므로
      전부 폐기된다. 의무가 없으면 영향의 근거도 없다.
    - **음수 인덱스**: 범위 밖이다. 파이썬의 음수 인덱싱으로 해석하지 않는다 — 모델이
      "뒤에서 첫 번째"를 의도했을 가능성보다 잘못 센 가능성이 압도적으로 크다.
    - **같은 문단이 두 번 영향으로 지목됨**: 검사하지 않는다. 중복은 거짓이 아니라
      장황함이며, 제거하면 모델이 무엇을 냈는지가 기록에서 사라진다 (gate 2단과 같은 판단).
    - **부서 근거 문단이 영향 문단이 아님**: 위반이 아니다. **설계상 정상이다** —
      골든셋 case-010 의 `경영지원부` 가 그 형태이며, 그 사실은 폐기가 아니라
      `basis_is_affected` 표시로 다뤄진다 (`prompts/impact.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from regchange.prompts.impact import ImpactDraft, ParagraphImpact


class ConsistencyRule(StrEnum):
    """검사 규칙 식별자. 어느 규칙이 몇 번 발화했는지 세기 위해 값으로 둔다."""

    OBLIGATION_INDEX_OUT_OF_RANGE = "OBLIGATION_INDEX_OUT_OF_RANGE"
    """영향 문단이 가리키는 의무사항 순번이 입력 목록에 없다. 연결이 끊긴 출력이다."""


class RuleStatus(StrEnum):
    """규칙이 지금 강제되는가, 아니면 관측만 하고 두는가."""

    ENFORCED = "ENFORCED"
    """위반 시 항목을 폐기한다."""
    REJECTED_BY_OBSERVATION = "REJECTED_BY_OBSERVATION"
    """전수 관측에서 **정답 출력에도 발화**했다. 만들지 않는다."""
    NOT_OBSERVED = "NOT_OBSERVED"
    """전수 관측에서 한 번도 발화하지 않았다. 상상으로 만들지 않는다."""


@dataclass(frozen=True, slots=True)
class EvaluatedRule:
    """검토한 규칙 후보 하나와 그 관측 결과.

    강제되지 않는 규칙까지 코드에 남기는 이유는 모듈 docstring 참조 — **기각의 근거가
    사라지면 같은 규칙이 다시 제안된다.**
    """

    name: str
    status: RuleStatus
    fired: int
    """골든셋 15건 + Opus 대조 4건의 원본 출력 21건 중 발화 수."""
    fired_on_correct: int
    """그중 정답에 해당하던 출력의 수. **0이 아니면 그 규칙은 쓸 수 없다.**"""
    note: str


EVALUATED_RULES: tuple[EvaluatedRule, ...] = (
    EvaluatedRule(
        name="action=NEW_PROVISION_REVIEW ∧ 인용>0",
        status=RuleStatus.REJECTED_BY_OBSERVATION,
        fired=12,
        fired_on_correct=10,
        note=(
            "기준선 §11 이 4단계 후보로 예고한 규칙이다. 21건 중 12건에서 발화했고 그중 "
            "10건이 정답 출력이었다. 한 개정에서 일부 의무는 기존 조항에 걸리고 일부는 "
            "담을 조항이 없는 것이 정상이므로, 두 값은 동시에 참일 수 있다"
        ),
    ),
    EvaluatedRule(
        name="action=NEW_PROVISION_REVIEW ∧ 모든 의무에 인용이 붙음",
        status=RuleStatus.REJECTED_BY_OBSERVATION,
        fired=5,
        fired_on_correct=3,
        note=(
            "위 규칙을 좁힌 형태. 5건 발화 중 3건(case-002·004·007)이 정답 문단을 "
            "적중한 출력이었다. 좁혀도 정답을 버린다"
        ),
    ),
    EvaluatedRule(
        name="obligation_type=EDITORIAL ∧ 인용>0",
        status=RuleStatus.REJECTED_BY_OBSERVATION,
        fired=2,
        fired_on_correct=1,
        note="case-009 는 EDITORIAL 인용 1건으로 정답을 맞혔고 case-011 은 틀렸다. 가르지 못한다",
    ),
    EvaluatedRule(
        name="status=INSUFFICIENT_EVIDENCE ∧ 인용>0",
        status=RuleStatus.NOT_OBSERVED,
        fired=0,
        fired_on_correct=0,
        note="4단계 지시가 예상 조합으로 든 것. 21건에서 한 번도 관측되지 않았다",
    ),
    EvaluatedRule(
        name="status=OK ∧ 인용=0",
        status=RuleStatus.NOT_OBSERVED,
        fired=0,
        fired_on_correct=0,
        note=(
            "관측 0건. 그리고 gate 2단이 통과한 근거로 상태를 다시 정하므로 "
            "이 조합은 최종 출력에 남을 수 없다"
        ),
    ),
    EvaluatedRule(
        name="action=TRANSFER_AND_CLOSE ∧ 인용>0",
        status=RuleStatus.NOT_OBSERVED,
        fired=0,
        fired_on_correct=0,
        note="관측 0건",
    ),
    EvaluatedRule(
        name="의무=0 ∧ action=NEW_PROVISION_REVIEW",
        status=RuleStatus.NOT_OBSERVED,
        fired=0,
        fired_on_correct=0,
        note="관측 0건",
    ),
    EvaluatedRule(
        name=ConsistencyRule.OBLIGATION_INDEX_OUT_OF_RANGE.value,
        status=RuleStatus.ENFORCED,
        fired=0,
        fired_on_correct=0,
        note=(
            "관측에서 나온 규칙이 아니다. **참일 수 없는 것**이므로 관측을 기다리지 않는다 — "
            "자기 입력에 없는 순번을 가리키는 출력은 어떤 경우에도 옳지 않다. "
            "위 조합들과 부류가 다르다"
        ),
    ),
)
"""검토한 규칙 후보 전부와 그 관측 결과. **기각된 것도 남긴다.**"""


@dataclass(frozen=True, slots=True)
class ConsistencyViolation:
    """폐기된 항목 하나와 그 사유."""

    rule: ConsistencyRule
    claim: str
    """폐기된 주장. 무엇을 버렸는지가 남아야 필터가 작동했는지 확인할 수 있다."""
    detail: str


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    """정합성 검사 결과. 남은 영향 문단과 폐기된 것을 함께 담는다."""

    kept: tuple[ParagraphImpact, ...]
    violations: tuple[ConsistencyViolation, ...]

    @property
    def clean(self) -> bool:
        """위반이 하나도 없었는가."""
        return not self.violations


def check_draft(draft: ImpactDraft, *, obligation_count: int) -> ConsistencyReport:
    """초안을 자기 입력과 대조해 연결이 끊긴 영향 문단을 폐기한다.

    목적:
        gate 3단(모델 호출) 앞에서, 코드만으로 확정할 수 있는 오류를 걷어낸다.

    구현 이유:
        `obligation_count` 를 인자로 받는다. 초안 안에는 의무 목록이 없고 순번만 있으므로,
        범위를 아는 것은 호출부다. 초안에 의무 목록을 복사해 넣으면 같은 사실이 두 곳에
        존재하게 되고 두 곳은 어긋난다.

    트레이드오프:
        폐기된 영향 문단에 딸린 통제항목도 함께 사라진다. 통제항목만 살리는 방법도 있으나,
        어느 조항에 대한 통제항목인지가 사라진 목록은 담당자가 쓸 수 없다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    kept: list[ParagraphImpact] = []
    violations: list[ConsistencyViolation] = []

    for impact in draft.impacts:
        if not 0 <= impact.obligation_index < obligation_count:
            violations.append(
                ConsistencyViolation(
                    rule=ConsistencyRule.OBLIGATION_INDEX_OUT_OF_RANGE,
                    claim=impact.claim,
                    detail=(
                        f"obligation_index={impact.obligation_index} 가 의무 "
                        f"{obligation_count}건의 범위 밖이다"
                    ),
                )
            )
            continue
        kept.append(impact)

    return ConsistencyReport(kept=tuple(kept), violations=tuple(violations))
