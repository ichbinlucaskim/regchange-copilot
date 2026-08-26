"""그래프 상태와 그 직렬화 — 프레임워크가 형태를 보존한다고 가정하지 않는다.

목적:
    LangGraph 노드 사이를 오가는 상태의 모양을 정의하고, 도메인 값을 상태에 넣고 꺼내는
    변환을 한곳에 모은다.

구현 이유:
    **상태에 도메인 객체를 그대로 넣지 않는다.** 체크포인터의 직렬화기가 dataclass 와
    pydantic 모델을 다루기는 하지만, 실측해 보니 **dataclass 의 `tuple` 필드가 역직렬화
    후 `list` 로 돌아온다.** 타입 힌트는 `tuple` 이라고 적혀 있는데 실제 값은 `list` 인
    상태가 되며, 이 어긋남은 아무 오류도 내지 않는다. 같은 직렬화기가 "등록되지 않은
    타입은 앞으로 차단된다"는 경고도 낸다 — 즉 **지금 되는 것이 다음 버전에서 안 될 수
    있다.**

    이것이 ADR-013 이 LangGraph 를 채택하며 경계한 바로 그 문제다 — *"프레임워크 동작에
    대한 잘못된 가정이 문제의 흔한 원인"*. 그래서 상태에는 **문자열·숫자·리스트·사전만**
    넣고, 도메인 값으로의 복원은 우리 파서가 한다. 프레임워크가 무엇을 보존하든 우리가
    아는 형태로 돌아온다.

    부수 효과가 하나 더 있다. 체크포인트가 **읽을 수 있는 문서**가 된다. 감사에서
    "그때 그래프가 무엇을 들고 있었는가"를 물으면 직렬화된 객체 덩어리가 아니라
    평범한 JSON 을 보여줄 수 있다 (원칙 6).

    **복원에 기존 파서를 재사용한다.** `parse_draft` 는 모델 출력 dict 를 읽는 함수이고,
    상태에 담는 dict 를 같은 모양으로 만들면 복원 경로가 하나가 된다. 모양이 두 개면
    한쪽만 고쳐지는 일이 생긴다.

트레이드오프:
    - 변환 코드가 는다. 그 대신 프레임워크 버전 업그레이드가 상태 호환성을 깨뜨릴 여지가
      줄고, 깨진다면 우리 파서에서 시끄럽게 깨진다.
    - 노드마다 복원 비용이 든다. 조문 한 건 규모에서는 무시할 수 있다.
    - 상태가 커진다(검색 결과 전체가 들어간다). 다시 검색하면 작아지지만, 그러면 **재개
      시점의 검색 결과가 최초 실행과 달라질 수 있다** — 승격이나 코퍼스 개정이 그 사이에
      끼면 인용 검증의 정답 집합이 바뀐다. 감사 근거가 흔들리는 것보다 상태가 큰 편이 낫다.

엣지 케이스:
    - **재개 시 상태 스키마가 바뀐 경우**: 없는 키는 `.get` 으로 읽어 기본값이 되고,
      필수 값이 없으면 노드가 실패한다. 조용히 진행하지 않는다.
    - **검색 결과가 0건**: `retrieval` 은 여전히 존재하며 `chunks` 가 빈 리스트다.
      키 자체를 빼지 않는다 — "검색하지 않았다"와 "찾지 못했다"는 다른 사실이다.
    - **초안이 없는 상태에서 검증 노드가 도는 경우**: 그래프 구조상 일어나지 않지만,
      복원 함수는 `None` 을 돌려주고 노드가 판단한다.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import Any, TypedDict

from regchange.prompts.impact import ImpactDraft, parse_draft
from regchange.retrieval.models import RetrievalResult
from regchange.verification.base import ClaimRelation, SupportLevel
from regchange.verification.grounding import ClaimJudgment, GroundingResult, decide


class ReviewDecisionKind(StrEnum):
    """검토자가 내릴 수 있는 판단 3종. DB CHECK 제약과 같은 값이어야 한다."""

    ACCEPT = "ACCEPT"
    """초안대로 승인한다."""
    EDIT = "EDIT"
    """수정해서 승인한다. **초안은 고치지 않고** 수정 내용을 승인 레코드에 남긴다."""
    REJECT = "REJECT"
    """반려한다. 사유 코드가 필수다."""


class AssessmentState(TypedDict, total=False):
    """그래프가 노드 사이에 들고 다니는 상태 전부.

    `total=False` 인 이유: 초기 입력에는 뒤 단계의 키가 없다. 없는 것과 비어 있는 것을
    구별해야 하며(`retrieval` 참조), 노드는 자기가 필요한 키가 없으면 실패한다.
    """

    # ── 입력 (load_change 가 채운다) ─────────────────────────────────
    assessment_id: str
    thread_id: str
    law_name: str
    article_path: str
    revision_kind: str
    change_type: str
    after_text: str
    before_text: str | None
    as_of: str
    document_versions: dict[str, str]

    # ── sanitize_input ──────────────────────────────────────────────
    injection_signals: list[str]
    trust_levels: dict[str, str]
    """입력 조각별 신뢰 등급 태깅. **R-23 은 여기서 해결되지 않는다** — 태깅만 하고
    스캔 범위를 제한하지 않는다. 범위 제한은 스키마에 신뢰 등급이 내려온 뒤(5단계)다."""

    # ── retrieve_policy ─────────────────────────────────────────────
    retrieval: dict[str, Any]

    # ── extract_obligations ─────────────────────────────────────────
    obligation_rows: list[list[str]]
    obligation_status: str
    obligation_discarded: list[dict[str, str]]

    # ── draft_impact / verify_citations ─────────────────────────────
    revision: int
    draft: dict[str, Any] | None
    previous_raw: str | None
    draft_failed: bool
    consistency_violations: list[dict[str, str]]
    gate_discarded: list[dict[str, str]]
    judgments: list[dict[str, str]]
    verification_error: str | None

    # ── 최종 판정 ───────────────────────────────────────────────────
    status: str
    persisted: bool
    queued: bool

    # ── human_review 이후 ───────────────────────────────────────────
    decision: dict[str, Any] | None
    decision_id: str | None
    outbox_ids: list[str]


def dump_retrieval(result: RetrievalResult) -> dict[str, Any]:
    """검색 결과를 상태에 넣을 수 있는 사전으로 만든다. 값을 잃지 않는다."""
    return result.model_dump(mode="json")


def load_retrieval(payload: dict[str, Any]) -> RetrievalResult:
    """상태의 사전을 검색 결과로 되돌린다. 형태 보존은 pydantic 이 검증한다."""
    return RetrievalResult.model_validate(payload)


def dump_draft(draft: ImpactDraft) -> dict[str, Any]:
    """초안을 **모델 출력과 같은 모양**의 사전으로 만든다.

    같은 모양으로 두는 이유는 복원에 `parse_draft` 를 그대로 쓰기 위해서다. 모양이 둘이면
    한쪽만 고쳐지는 일이 생기고, 그 어긋남은 재개 시점에야 드러난다.
    """
    payload = dataclasses.asdict(draft)
    payload["impacts"] = [
        {
            "paragraph_id": impact.paragraph_id,
            "quote": impact.quote,
            "claim": impact.claim,
            "obligation_index": impact.obligation_index,
            "control_items": list(impact.control_items),
        }
        for impact in draft.impacts
    ]
    payload["departments"] = [
        {
            "department": entry.department,
            "basis_paragraph_id": entry.basis_paragraph_id,
            "basis_quote": entry.basis_quote,
            "derivation": entry.derivation.value,
            "rationale": entry.rationale,
        }
        for entry in draft.departments
    ]
    payload["required_evidence"] = list(draft.required_evidence)
    payload["status"] = draft.status.value
    payload["obligation_type"] = draft.obligation_type.value
    payload["risk_level"] = draft.risk_level.value
    payload["confidence"] = draft.confidence.value
    return payload


def load_draft(payload: dict[str, Any] | None) -> ImpactDraft | None:
    """상태의 사전을 초안으로 되돌린다. 없으면 None — 노드가 판단한다."""
    if payload is None:
        return None
    return parse_draft(payload)


def dump_judgments(grounding: GroundingResult) -> list[dict[str, str]]:
    """Gate 3단 판정을 상태에 넣을 수 있는 목록으로 만든다.

    de-anchored 의 원본 판정(`relation`)도 함께 싣는다. 사상된 `level` 만 남기면
    `BEYOND` 와 `UNRELATED` 가 같은 값이 되고, 검토 화면이 "무엇을 넘어섰는지"를
    보여줄 수 없다.
    """
    return [
        {
            "key": j.key,
            "level": j.level.value,
            "reason": j.reason,
            **({"relation": j.relation.value} if j.relation else {}),
        }
        for j in grounding.judgments
    ]


def load_judgments(payload: list[dict[str, str]] | None) -> GroundingResult:
    """상태의 판정 목록을 집계 결과로 되돌린다. 없으면 빈 결과다."""
    return decide(
        ClaimJudgment(
            key=str(item["key"]),
            level=SupportLevel(str(item["level"])),
            reason=str(item.get("reason", "")),
            relation=ClaimRelation(item["relation"]) if item.get("relation") else None,
        )
        for item in payload or []
    )
