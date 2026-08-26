"""gate 2단(초안 인용)·3단(의미 뒷받침) 집계와 정합성 필터의 규칙을 고정한다.

이 테스트가 존재하는 이유: 이 세 모듈이 **무엇을 통과시키고 무엇을 버리는가**가 이
시스템의 안전 성질 전부다. 규칙이 조용히 느슨해지면 근거 없는 제안이 담당자에게 도달하고,
그 사실은 출력만 봐서는 드러나지 않는다.
"""

from __future__ import annotations

from regchange.guards.citations import DiscardReason, GateStatus, enforce_draft_citations
from regchange.guards.consistency import (
    EVALUATED_RULES,
    ConsistencyRule,
    RuleStatus,
    check_draft,
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
from regchange.verification.base import SupportLevel
from regchange.verification.grounding import ClaimJudgment, decide

PARAGRAPH = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
TEXT = "정보보호부장은 침해사고를 인지한 즉시 정보보호 최고책임자에게 보고하여야 한다."


def _draft(
    *,
    impacts: tuple[ParagraphImpact, ...] = (),
    departments: tuple[DepartmentAssignment, ...] = (),
    status: DraftStatus = DraftStatus.DRAFT,
) -> ImpactDraft:
    return ImpactDraft(
        status=status,
        obligation_type=ObligationType.NEW,
        risk_level=RiskLevel.MEDIUM,
        risk_reason="",
        confidence=Confidence.MEDIUM,
        summary="",
        reason="",
        impacts=impacts,
        departments=departments,
        required_evidence=(),
    )


def _impact(paragraph_id: str, quote: str, *, index: int = 0) -> ParagraphImpact:
    return ParagraphImpact(
        paragraph_id=paragraph_id,
        quote=quote,
        claim="이 조항의 보고 시점을 고쳐야 한다",
        obligation_index=index,
        control_items=("보고 시점 확인",),
    )


# ---------------------------------------------------------------------------
# gate 2단 — 초안의 인용과 **부서 근거**를 같은 규칙으로 대조한다
# ---------------------------------------------------------------------------


def test_department_basis_is_also_checked() -> None:
    """부서 배정 근거도 인용이다. 지어낸 근거로 배정된 부서는 폐기된다."""
    draft = _draft(
        impacts=(_impact(PARAGRAPH, "정보보호부장은 침해사고를 인지한 즉시"),),
        departments=(
            DepartmentAssignment(
                department="홍보부",
                basis_paragraph_id=PARAGRAPH,
                basis_quote="홍보부와 협의하여 안내 여부를 정한다",  # 원문에 없다
                derivation=DepartmentDerivation.CONSULTATION,
                rationale="",
            ),
        ),
    )

    result = enforce_draft_citations(draft, retrieved={PARAGRAPH: TEXT}, searched_scope=())

    assert result.status is GateStatus.OK
    assert len(result.impacts) == 1
    assert result.departments == ()
    assert [d.reason for d in result.discarded] == [DiscardReason.QUOTE_NOT_FOUND]


def test_department_basis_need_not_be_an_affected_paragraph() -> None:
    """**간접 도출은 폐기 사유가 아니다** (골든셋 case-010 의 경영지원부 형태)."""
    draft = _draft(
        impacts=(_impact(PARAGRAPH, "정보보호부장은 침해사고를 인지한 즉시"),),
        departments=(
            DepartmentAssignment(
                department="경영지원부",
                basis_paragraph_id=OTHER,
                basis_quote="예산은 경영지원부장이 편성한다",
                derivation=DepartmentDerivation.SUBJECT_IN_TEXT,
                rationale="",
            ),
        ),
    )

    result = enforce_draft_citations(
        draft,
        retrieved={PARAGRAPH: TEXT, OTHER: "예산은 경영지원부장이 편성한다."},
        searched_scope=(),
    )

    assert [d.department for d in result.departments] == ["경영지원부"]
    assert result.discarded == ()
    assert draft.basis_is_affected(result.departments[0]) is False


def test_no_surviving_impact_drops_departments_too() -> None:
    """영향 문단이 하나도 없으면 부서만 남기지 않는다. 절반짜리 평가는 쓸 수 없다."""
    draft = _draft(
        impacts=(_impact(PARAGRAPH, "존재하지 않는 인용문"),),
        departments=(
            DepartmentAssignment(
                department="정보보호부",
                basis_paragraph_id=PARAGRAPH,
                basis_quote="정보보호부장은",
                derivation=DepartmentDerivation.SUBJECT_IN_TEXT,
                rationale="",
            ),
        ),
    )

    result = enforce_draft_citations(draft, retrieved={PARAGRAPH: TEXT}, searched_scope=())

    assert result.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert result.impacts == ()
    assert result.departments == ()


def test_unknown_paragraph_is_not_retrieved() -> None:
    """검색 결과 밖 문단을 인용하면 폐기한다 (원칙 2)."""
    draft = _draft(impacts=(_impact(OTHER, "무엇이든"),))

    result = enforce_draft_citations(draft, retrieved={PARAGRAPH: TEXT}, searched_scope=())

    assert [d.reason for d in result.discarded] == [DiscardReason.NOT_RETRIEVED]


# ---------------------------------------------------------------------------
# 정합성 필터 — 관측이 허락한 규칙만 강제한다
# ---------------------------------------------------------------------------


def test_obligation_index_out_of_range_is_dropped() -> None:
    """자기 입력에 없는 의무 순번을 가리키면 그 영향 문단은 연결이 끊긴 것이다."""
    draft = _draft(impacts=(_impact(PARAGRAPH, "x", index=3),))

    report = check_draft(draft, obligation_count=2)

    assert report.kept == ()
    assert [v.rule for v in report.violations] == [ConsistencyRule.OBLIGATION_INDEX_OUT_OF_RANGE]


def test_negative_index_is_out_of_range() -> None:
    """음수를 파이썬의 역인덱싱으로 해석하지 않는다. 잘못 센 것으로 본다."""
    report = check_draft(_draft(impacts=(_impact(PARAGRAPH, "x", index=-1),)), obligation_count=2)

    assert report.violations


def test_valid_index_passes() -> None:
    """범위 안이면 통과한다."""
    report = check_draft(_draft(impacts=(_impact(PARAGRAPH, "x", index=1),)), obligation_count=2)

    assert report.clean
    assert len(report.kept) == 1


def test_rejected_rules_are_kept_as_evidence() -> None:
    """**기각된 규칙도 코드에 남는다.**

    기각의 근거가 사라지면 다음 사람이 같은 규칙을 다시 제안하고 같은 측정을 반복한다.
    정답 출력에도 발화한 규칙은 강제되지 않아야 한다.
    """
    enforced = {r.name for r in EVALUATED_RULES if r.status is RuleStatus.ENFORCED}
    rejected = [r for r in EVALUATED_RULES if r.status is RuleStatus.REJECTED_BY_OBSERVATION]

    assert enforced == {ConsistencyRule.OBLIGATION_INDEX_OUT_OF_RANGE.value}
    assert rejected, "전수 관측 결과가 코드에서 사라졌다"
    for rule in rejected:
        assert rule.fired_on_correct > 0, (
            f"{rule.name}: 정답에 발화하지 않았다면 기각 사유가 다르다"
        )
    for rule in EVALUATED_RULES:
        if rule.status is RuleStatus.ENFORCED:
            continue
        assert rule.fired_on_correct > 0 or rule.fired == 0


# ---------------------------------------------------------------------------
# gate 3단 집계 — 임계치를 만들지 않는다
# ---------------------------------------------------------------------------


def test_unsupported_triggers_rewrite_and_warning() -> None:
    """뒷받침되지 않은 주장이 하나라도 있으면 재작성 대상이고 경고 대상이다."""
    result = decide(
        [
            ClaimJudgment(key="impact:0", level=SupportLevel.SUPPORTED, reason=""),
            ClaimJudgment(key="impact:1", level=SupportLevel.UNSUPPORTED, reason="소재만 겹친다"),
        ]
    )

    assert result.needs_rewrite is True
    assert result.warn is True
    assert result.unsupported_keys == ("impact:1",)
    assert result.unsupported_notes == ("impact:1: 소재만 겹친다",)


def test_partial_does_not_trigger_rewrite() -> None:
    """`PARTIAL` 은 날조가 아니다. 제거하지도 재작성하지도 않는다."""
    result = decide([ClaimJudgment(key="impact:0", level=SupportLevel.PARTIAL, reason="")])

    assert result.needs_rewrite is False
    assert result.unsupported_keys == ()
    assert result.counts[SupportLevel.PARTIAL] == 1


def test_empty_judgments_is_not_a_failure() -> None:
    """판정이 0건인 것은 검증 실패가 아니라 판정할 것이 없었던 것이다."""
    result = decide([])

    assert result.needs_rewrite is False
    assert result.unsupported_ratio is None, "0.0 은 '전부 뒷받침됐다'와 구별되지 않는다"


def test_counts_include_zero_levels() -> None:
    """0인 등급도 키로 존재한다. 빠진 키는 0과 구별되지 않는다."""
    result = decide([ClaimJudgment(key="a", level=SupportLevel.SUPPORTED, reason="")])

    assert set(result.counts) == set(SupportLevel)
    assert result.counts[SupportLevel.UNSUPPORTED] == 0


# ---------------------------------------------------------------------------
# 최종 판정 — gate 2단과 같은 규칙을 gate 3단 뒤에도 적용한다
# ---------------------------------------------------------------------------


def test_finalize_drops_departments_when_no_impact_survives() -> None:
    """영향 주장이 전부 떨어지면 부서도 남기지 않는다.

    gate 2단이 이미 같은 규칙을 쓴다. 여기 없으면 **"모른다"고 판정한 평가가 부서 목록을
    달고 나간다** — de-anchored 대조에서 실제로 관측된 상태다 (case-005).
    """
    from regchange.pipeline.impact import AssessmentStatus, finalize

    draft = _draft(
        impacts=(_impact(PARAGRAPH, "정보보호부장은"),),
        departments=(
            DepartmentAssignment(
                department="정보보호부",
                basis_paragraph_id=PARAGRAPH,
                basis_quote="정보보호부장은",
                derivation=DepartmentDerivation.SUBJECT_IN_TEXT,
                rationale="",
            ),
        ),
    )
    grounding = decide([ClaimJudgment(key="impact:0", level=SupportLevel.UNSUPPORTED, reason="")])

    final, status = finalize(draft, grounding, draft_status=DraftStatus.DRAFT)

    assert status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert final.impacts == ()
    assert final.departments == (), "영향 없는 평가가 부서만 달고 나갔다"


def test_finalize_keeps_departments_when_an_impact_survives() -> None:
    """영향이 하나라도 남으면 뒷받침된 부서는 유지된다."""
    from regchange.pipeline.impact import AssessmentStatus, finalize

    draft = _draft(
        impacts=(_impact(PARAGRAPH, "a"), _impact(OTHER, "b", index=1)),
        departments=(
            DepartmentAssignment(
                department="정보보호부",
                basis_paragraph_id=PARAGRAPH,
                basis_quote="정보보호부장은",
                derivation=DepartmentDerivation.SUBJECT_IN_TEXT,
                rationale="",
            ),
        ),
    )
    grounding = decide(
        [
            ClaimJudgment(key="impact:0", level=SupportLevel.SUPPORTED, reason=""),
            ClaimJudgment(key="impact:1", level=SupportLevel.UNSUPPORTED, reason=""),
        ]
    )

    final, status = finalize(draft, grounding, draft_status=DraftStatus.DRAFT)

    assert status is AssessmentStatus.NEEDS_REVIEW
    assert len(final.impacts) == 1
    assert len(final.departments) == 1
