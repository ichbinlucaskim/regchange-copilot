"""그래프 상태의 직렬화가 **형태를 잃지 않는지** 확인한다.

이 테스트가 존재하는 이유: 체크포인터의 직렬화기가 dataclass 의 `tuple` 을 `list` 로
되돌리는 것을 실측했다(ADR-018). 타입 힌트는 `tuple` 인데 값은 `list` 인 상태는 아무 오류도
내지 않으며, 재개한 그래프가 다른 형태의 값으로 도는 것을 아무도 모른다.

그래서 상태에는 평범한 사전만 넣고 복원은 우리 파서가 한다. **그 왕복이 값을 잃지 않는지가
이 테스트의 전부다** — 잃으면 재개 시점에야 드러나고, 그때는 승인 대기 중인 평가가 깨진다.
"""

from __future__ import annotations

import datetime as dt
import json
from uuid import uuid4

from regchange.graph.state import (
    dump_draft,
    dump_judgments,
    dump_retrieval,
    load_draft,
    load_judgments,
    load_retrieval,
)
from regchange.prompts.impact import (
    Confidence,
    DepartmentAssignment,
    DepartmentDerivation,
    DraftStatus,
    ImpactDraft,
    ParagraphImpact,
    RiskLevel,
)
from regchange.prompts.obligation import ObligationType
from regchange.retrieval.models import (
    DelegationReport,
    PromotionBasis,
    PromotionMechanism,
    RetrievalResult,
    RetrievalSource,
    RetrievedChunk,
    SearchMode,
)
from regchange.verification.base import SupportLevel
from regchange.verification.grounding import ClaimJudgment, decide

DRAFT = ImpactDraft(
    status=DraftStatus.NEEDS_REVIEW,
    obligation_type=ObligationType.STRENGTHENED,
    risk_level=RiskLevel.HIGH,
    risk_reason="대외 신고 기한이 명문화됐다",
    confidence=Confidence.MEDIUM,
    summary="신고 기한 정비 필요",
    reason="판단이 갈리는 지점이 있다",
    impacts=(
        ParagraphImpact(
            paragraph_id="11111111-1111-1111-1111-111111111111",
            quote="정보보호부장은 침해사고를 인지한 즉시",
            claim="신고 시점을 24시간 기준으로 고쳐야 한다",
            obligation_index=0,
            control_items=("신고 시점 문구 정비", "보고선 확인"),
        ),
    ),
    departments=(
        DepartmentAssignment(
            department="정보보호부",
            basis_paragraph_id="11111111-1111-1111-1111-111111111111",
            basis_quote="정보보호부장은",
            derivation=DepartmentDerivation.SUBJECT_IN_TEXT,
            rationale="조항이 주체를 명시한다",
        ),
    ),
    required_evidence=("신고 이력", "보고 대장"),
)


def test_draft_survives_a_json_round_trip() -> None:
    """초안이 **JSON 을 통과해도** 값과 형태가 그대로다.

    `json.dumps/loads` 를 실제로 거치는 것이 요점이다. 체크포인트는 우리 프로세스 밖에
    저장되므로, 파이썬 객체 참조가 아니라 직렬화 가능한 값이어야 한다.
    """
    restored = load_draft(json.loads(json.dumps(dump_draft(DRAFT), ensure_ascii=False)))

    assert restored == DRAFT
    assert restored is not None
    assert isinstance(restored.impacts, tuple), "tuple 이 list 로 돌아왔다 (ADR-018 의 실측 문제)"
    assert isinstance(restored.impacts[0].control_items, tuple)
    assert restored.risk_level is RiskLevel.HIGH
    assert restored.departments[0].derivation is DepartmentDerivation.SUBJECT_IN_TEXT


def test_retrieval_survives_with_promotion_marks() -> None:
    """검색 결과가 **승격 표시까지** 보존된다.

    승격 표시가 사라지면 검토 화면이 "왜 이게 여기 있나"에 답할 수 없고, 측정이 승격분을
    분리해 셀 수 없다.
    """
    chunk = RetrievedChunk(
        paragraph_id=uuid4(),
        doc_id="ISP-POL-001",
        doc_version="3.2",
        article_no=5,
        article_title="경영진의 책임",
        text_raw="경영진은 …",
        score=0.0,
        rank=11,
        source=RetrievalSource.DELEGATION_PROMOTED,
        promotion=PromotionBasis(
            via_doc_id="ISP-GUIDE-003",
            via_article_no=5,
            via_rank=3,
            delegation_quote="이 지침은 「정보보호정책」(ISP-POL-001)에서 위임된 사항 …",
            mechanism=PromotionMechanism.RESEARCHED,
        ),
    )
    result = RetrievalResult(
        mode=SearchMode.HYBRID,
        as_of=dt.date(2026, 2, 1),
        chunks=(chunk,),
        searched_scope=("ISP-POL-001 v3.2",),
        corpus_size=152,
        delegation=DelegationReport(
            top_n=2,
            promoted=1,
            used_edges=("ISP-GUIDE-003 → ISP-POL-001",),
            declared_article_edges=(),
            skipped_dangling=(),
            undeclared_docs=("ISP-POL-001",),
        ),
    )

    restored = load_retrieval(json.loads(json.dumps(dump_retrieval(result), ensure_ascii=False)))

    assert restored == result
    assert restored.promoted[0].promotion is not None
    assert restored.promoted[0].promotion.mechanism is PromotionMechanism.RESEARCHED
    assert restored.delegation is not None
    assert restored.delegation.undeclared_docs == ("ISP-POL-001",)
    assert isinstance(restored.searched_scope, tuple)


def test_judgments_survive_and_keep_reasons() -> None:
    """판정과 **그 이유**가 보존된다. 이유는 재작성 프롬프트가 그대로 받는다."""
    grounding = decide(
        [
            ClaimJudgment(key="impact:0", level=SupportLevel.UNSUPPORTED, reason="소재만 겹친다"),
            ClaimJudgment(key="dept:0", level=SupportLevel.PARTIAL, reason="일부만 담고 있다"),
        ]
    )

    restored = load_judgments(json.loads(json.dumps(dump_judgments(grounding), ensure_ascii=False)))

    assert restored.unsupported_keys == ("impact:0",)
    assert restored.unsupported_notes == ("impact:0: 소재만 겹친다",)
    assert restored.counts[SupportLevel.PARTIAL] == 1


def test_empty_judgment_list_is_not_none() -> None:
    """판정이 없는 상태를 복원해도 빈 결과이지 실패가 아니다."""
    assert load_judgments(None).judgments == ()
    assert load_judgments([]).needs_rewrite is False


def test_missing_draft_is_none_not_an_exception() -> None:
    """초안이 없는 상태는 None 이다. 노드가 판단하고 여기서 예외를 던지지 않는다."""
    assert load_draft(None) is None
