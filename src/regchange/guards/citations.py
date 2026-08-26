"""근거 강제 gate 2단 — 인용을 코드로 대조하고 폐기한다 (원칙 2).

**이 모듈이 gate 의 본체다. LLM 에게 묻지 않는다. 순수 함수다.**

목적:
    모델이 제시한 인용이 (a) 이번 실행에서 실제로 검색된 문단 집합에 속하는지,
    (b) 인용문이 그 문단 원문에 실재하는지를 대조하고, 통과하지 못한 인용을 폐기한다.
    인용이 전부 폐기된 주장은 제거한다.

구현 이유:
    **프롬프트 지시가 아니라 구조여야 한다.** "출처를 밝히세요"는 지켜지지 않을 수 있고,
    지켜지지 않았다는 사실이 출력만 봐서는 드러나지 않는다. 집합 연산은 지켜진다.

    **ID 대조와 인용문 대조를 둘 다 한다.** ID 만 보면 "실재하는 문단 ID + 지어낸 인용문"이
    통과한다. 그 조합이 가장 위험하다 — 담당자가 ID 를 눌러 원문을 열기 전까지는 완벽히
    그럴듯해 보이고, 열어 봐야 알 수 있는 오류는 검증 비용을 사람에게 전가한다.
    둘을 다른 사유로 기록하는 이유는 **어느 실패가 실제로 일어나는지 세기 위해서**다.

    **인용문 대조를 정규화 텍스트로 한다** (ADR-002). 모델이 공백이나 줄바꿈을 다르게
    옮겨 적는 것은 날조가 아니다. 원문 그대로 비교하면 그런 사소한 차이가 폐기 사유가
    되고, 그러면 gate 가 실제 위험이 아니라 서식을 잡게 된다.

    **"인용 0건"의 두 종류를 구별한다.**

    | | 무엇인가 | 처리 |
    |---|---|---|
    | 처음부터 0건 | 모델이 "대응 조항이 없다"고 말한 것 | **주장 유지**, 근거 없음으로 분류 |
    | 폐기 후 0건 | 근거를 제시했으나 전부 실재하지 않았다 | **주장을 제거** |

    같은 0건으로 뭉개면 담당자가 잘못된 작업을 한다 — 전자는 "우리에게 걸리는 새 의무인데
    담을 조항이 없다"(골든셋 case-013)이고 후자는 날조다. 이 저장소가 "실패한 날"과
    "실행하지 않은 날"을 구별한 것(ADR-014)과 같은 성질의 판단이다.

    **최종 상태를 모델이 아니라 여기서 정한다.** 모델의 `status` 는 참고 신호다. 모델이
    "충분하다"고 말해도 근거가 전부 폐기되면 충분하지 않다.

    **영향평가 초안에도 같은 대조를 적용한다** (`enforce_draft_citations`). 대조 대상이
    의무사항의 인용에서 **영향 문단의 인용과 부서 배정의 근거**로 넓어졌다. 부서 배정을
    빼면 "근거 없이 배정된 부서"가 gate 를 그냥 통과하고, 그 부서는 티켓을 받고 나서야
    근거가 없다는 것을 알게 된다 — 검증 비용을 조직 밖으로 전가하는 형태다.
    **판정 규칙(`_judge`)은 하나를 공유한다.** 둘로 나누면 한쪽만 조여지는 일이 생긴다.

트레이드오프:
    - **의미 뒷받침 검증(3단)을 하지 않는다.** 인용된 문단이 그 주장을 실제로 뒷받침하는지는
      집합 연산으로 알 수 없다. 그것은 evaluator 의 일이다 (`verification/grounding.py`).
      즉 이 gate 를 통과한 인용도 **틀릴 수 있다.** 보장하는 것은 "실재한다"까지다.
      골든셋 case-013 이 그 한계를 실측으로 보여준 사례다.
    - 인용문 대조가 부분 문자열 포함 여부다. 모델이 문단의 아무 조각이나 인용하면 통과한다.
      더 엄격하게(문장 경계 일치) 만들면 정당한 인용도 떨어진다. 지금은 느슨한 쪽을
      택하고, **어느 쪽 오류가 실제로 나오는지 재고 나서** 조인다.
    - 주장을 제거할 뿐 고쳐 쓰지 않는다. 고쳐 쓰면 검증기가 생성기가 된다 (원칙 3).

엣지 케이스:
    - 검색 결과가 0건: 모든 인용이 폐기되고 `INSUFFICIENT_EVIDENCE` 가 된다.
      "영향 없음"으로 판정하지 않는다 — 0건과 영향 없음은 다른 값이다 (ADR-013 엣지 케이스).
    - 같은 문단을 두 번 인용: 중복을 제거하지 않는다. 중복 인용은 위험하지 않고, 제거하면
      모델이 무엇을 냈는지가 기록에서 사라진다.
    - `paragraph_id` 가 UUID 형식이 아님: 존재하지 않는 ID 이므로 폐기 사유 `NOT_RETRIEVED`
      로 처리한다. 형식 오류를 별도 사유로 두지 않는 이유는 결과가 같기 때문이다.
    - 인용문이 빈 문자열: `EMPTY_QUOTE` 로 폐기한다. 빈 인용은 근거가 아니다.
    - 통과한 주장이 있는데 모델이 `INSUFFICIENT_EVIDENCE` 라고 말한 경우: **코드를 따른다.**
      근거가 실재하면 충분하다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from regchange.prompts.impact import (
    DepartmentAssignment,
    ImpactDraft,
    ParagraphImpact,
)
from regchange.prompts.obligation import (
    Citation,
    Obligation,
    ObligationExtraction,
    SuggestedAction,
)

_WHITESPACE = re.compile(r"\s+")


class DiscardReason(StrEnum):
    """인용이 폐기된 이유. 어느 실패가 실제로 일어나는지 세기 위해 구별한다."""

    NOT_RETRIEVED = "NOT_RETRIEVED"
    """인용한 문단 ID 가 이번 실행의 검색 결과에 없다. 지어낸 출처다."""
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    """문단 ID 는 실재하지만 인용문이 그 문단에 없다. **가장 위험한 형태다.**"""
    EMPTY_QUOTE = "EMPTY_QUOTE"
    """인용문이 비어 있다."""


class GateStatus(StrEnum):
    """gate 통과 후의 최종 상태. 모델이 아니라 코드가 정한다."""

    OK = "OK"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class DiscardedCitation:
    """폐기된 인용 하나와 그 사유. 폐기 사실도 기록된다."""

    obligation_summary: str
    citation: Citation
    reason: DiscardReason


@dataclass(frozen=True, slots=True)
class GateResult:
    """gate 2단의 판정 결과 전체."""

    status: GateStatus
    supported: tuple[Obligation, ...]
    """근거가 실재하는 주장. 인용은 통과한 것만 남아 있다."""
    unsupported: tuple[Obligation, ...]
    """모델이 처음부터 인용 없이 제출한 주장. 날조가 아니므로 제거하지 않는다."""
    discarded: tuple[DiscardedCitation, ...]
    """폐기된 인용. 무엇을 왜 버렸는지가 남아야 gate 가 작동했는지 확인할 수 있다."""
    removed: tuple[Obligation, ...]
    """인용을 제시했으나 전부 폐기되어 제거된 주장."""
    reason: str
    suggested_action: SuggestedAction
    searched_scope: tuple[str, ...]

    @property
    def citation_count(self) -> int:
        """통과한 인용 수. 0이면 `status` 는 반드시 `INSUFFICIENT_EVIDENCE` 다."""
        return sum(len(o.citations) for o in self.supported)


def _normalize(text: str) -> str:
    """공백만 접는다. 인용문 대조에 쓰며 다른 변형은 하지 않는다 (ADR-002)."""
    return _WHITESPACE.sub(" ", text).strip()


def enforce_citations(
    extraction: ObligationExtraction,
    *,
    retrieved: Mapping[str, str],
    searched_scope: tuple[str, ...],
) -> GateResult:
    """인용을 검색 결과 집합과 대조해 폐기하고 최종 상태를 판정한다.

    목적:
        원칙 2를 코드로 강제한다. 근거 없는 제안이 담당자에게 도달하는 경로를 없앤다.

    구현 이유:
        `retrieved` 를 `{문단 ID: 원문}` 매핑으로 받는다. ID 집합만 받으면 인용문 대조를
        할 수 없고, `RetrievedChunk` 객체를 받으면 이 모듈이 검색 계층에 결합된다.
        gate 는 검색이 어떻게 이루어졌는지 몰라야 한다 — 알면 검색 방식이 바뀔 때마다
        gate 를 고치게 되고, 그때 폐기 규칙이 함께 흔들린다.

    트레이드오프:
        `searched_scope` 를 그대로 실어 나르기만 한다. 이 모듈이 쓰지 않는 값이지만,
        `INSUFFICIENT_EVIDENCE` 출력에 "어디까지 찾아봤는가"가 필수이므로(3단계 §6)
        결과 객체가 그 답을 갖고 있어야 한다. 호출부가 다시 조립하면 빠뜨릴 수 있다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    normalized = {key: _normalize(value) for key, value in retrieved.items()}

    supported: list[Obligation] = []
    unsupported: list[Obligation] = []
    removed: list[Obligation] = []
    discarded: list[DiscardedCitation] = []

    for obligation in extraction.obligations:
        if not obligation.citations:
            # 처음부터 0건 — 모델이 "대응 조항이 없다"고 말한 것이다. 제거하지 않는다.
            unsupported.append(obligation)
            continue

        kept: list[Citation] = []
        for citation in obligation.citations:
            reason = _judge(citation, normalized)
            if reason is None:
                kept.append(citation)
            else:
                discarded.append(
                    DiscardedCitation(
                        obligation_summary=obligation.summary,
                        citation=citation,
                        reason=reason,
                    )
                )

        if kept:
            supported.append(
                Obligation(
                    obligation_type=obligation.obligation_type,
                    summary=obligation.summary,
                    source_span=obligation.source_span,
                    citations=tuple(kept),
                )
            )
        else:
            # 근거를 제시했으나 전부 실재하지 않았다. 주장을 제거한다.
            removed.append(obligation)

    status = GateStatus.OK if supported else GateStatus.INSUFFICIENT_EVIDENCE
    return GateResult(
        status=status,
        supported=tuple(supported),
        unsupported=tuple(unsupported),
        discarded=tuple(discarded),
        removed=tuple(removed),
        reason=extraction.reason,
        suggested_action=extraction.suggested_action,
        searched_scope=searched_scope,
    )


def _judge(citation: Citation, normalized: Mapping[str, str]) -> DiscardReason | None:
    """인용 하나를 판정한다. 통과하면 None, 아니면 폐기 사유."""
    return _judge_pair(citation.paragraph_id, citation.quote, normalized)


def _judge_pair(
    paragraph_id: str, quote: str, normalized: Mapping[str, str]
) -> DiscardReason | None:
    """`(문단 ID, 인용문)` 한 쌍을 판정한다. 의무사항과 초안이 같은 규칙을 쓴다."""
    if not quote.strip():
        return DiscardReason.EMPTY_QUOTE
    text = normalized.get(paragraph_id)
    if text is None:
        return DiscardReason.NOT_RETRIEVED
    if _normalize(quote) not in text:
        return DiscardReason.QUOTE_NOT_FOUND
    return None


class DraftItemKind(StrEnum):
    """초안에서 폐기된 항목의 종류. 어느 쪽이 실제로 떨어지는지 세기 위해 구별한다."""

    IMPACT = "IMPACT"
    """영향 문단 주장."""
    DEPARTMENT = "DEPARTMENT"
    """부서 배정의 근거."""


@dataclass(frozen=True, slots=True)
class DiscardedDraftItem:
    """초안에서 폐기된 항목 하나와 그 사유."""

    kind: DraftItemKind
    label: str
    """무엇이 폐기됐는지 사람이 읽는 값 — 주장 앞부분이나 부서 이름."""
    paragraph_id: str
    quote: str
    reason: DiscardReason


@dataclass(frozen=True, slots=True)
class DraftGateResult:
    """영향평가 초안에 대한 gate 2단 판정 결과."""

    status: GateStatus
    impacts: tuple[ParagraphImpact, ...]
    departments: tuple[DepartmentAssignment, ...]
    discarded: tuple[DiscardedDraftItem, ...]
    searched_scope: tuple[str, ...]

    @property
    def citation_count(self) -> int:
        """통과한 인용 수 — 영향 문단과 부서 근거를 합친다.

        0이면 상태는 반드시 `INSUFFICIENT_EVIDENCE` 다.
        """
        return len(self.impacts) + len(self.departments)


def enforce_draft_citations(
    draft: ImpactDraft,
    *,
    retrieved: Mapping[str, str],
    searched_scope: tuple[str, ...],
) -> DraftGateResult:
    """영향평가 초안의 인용을 검색 결과 집합과 대조해 폐기하고 상태를 판정한다.

    목적:
        원칙 2를 초안에도 강제한다. **부서 배정의 근거까지** 같은 대조를 받는다.

    구현 이유:
        영향 문단이 전부 폐기되면 `INSUFFICIENT_EVIDENCE` 다. **부서가 남아 있어도
        그렇다** — 부서만 있고 영향 문단이 없는 평가는 "누가 무엇을 해야 하는지"의
        절반이 비어 있고, 그 절반은 담당자가 채울 수 없다.

        부서는 영향 문단이 살아남았을 때만 유지한다. 근거 문단 자체는 영향 문단이 아니어도
        되지만(case-010), 영향이 하나도 없는데 부서만 남는 것은 다른 이야기다.

    트레이드오프:
        폐기 단위가 항목 하나다. 초안 전체를 버리지 않는다 — 하나가 떨어졌다고 전부
        버리면 근거가 실재하는 나머지가 함께 사라지고, 그 폐기는 "영향 없음"으로 보인다.
        3단계에서 주장 단위로 제거한 것과 같은 판단이다.

    엣지 케이스:
        - 검색 결과 0건: 전부 폐기되고 `INSUFFICIENT_EVIDENCE` 다.
        - 같은 문단을 여러 주장이 인용: 각각 판정한다. 중복을 제거하지 않는다.
        - 부서 근거가 영향 문단이 아님: **정상이다.** 폐기 사유가 아니다.
    """
    normalized = {key: _normalize(value) for key, value in retrieved.items()}
    discarded: list[DiscardedDraftItem] = []

    impacts: list[ParagraphImpact] = []
    for impact in draft.impacts:
        reason = _judge_pair(impact.paragraph_id, impact.quote, normalized)
        if reason is None:
            impacts.append(impact)
        else:
            discarded.append(
                DiscardedDraftItem(
                    kind=DraftItemKind.IMPACT,
                    label=impact.claim[:80],
                    paragraph_id=impact.paragraph_id,
                    quote=impact.quote,
                    reason=reason,
                )
            )

    departments: list[DepartmentAssignment] = []
    for entry in draft.departments:
        reason = _judge_pair(entry.basis_paragraph_id, entry.basis_quote, normalized)
        if reason is None:
            departments.append(entry)
        else:
            discarded.append(
                DiscardedDraftItem(
                    kind=DraftItemKind.DEPARTMENT,
                    label=entry.department,
                    paragraph_id=entry.basis_paragraph_id,
                    quote=entry.basis_quote,
                    reason=reason,
                )
            )

    status = GateStatus.OK if impacts else GateStatus.INSUFFICIENT_EVIDENCE
    return DraftGateResult(
        status=status,
        impacts=tuple(impacts),
        # 영향 문단이 하나도 없으면 부서 배정만 남기지 않는다 (구현 이유 참조).
        departments=tuple(departments) if impacts else (),
        discarded=tuple(discarded),
        searched_scope=searched_scope,
    )
