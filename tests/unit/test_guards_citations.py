"""gate 2단이 실제로 폐기하는지 검사한다 (원칙 2).

이 테스트가 존재하는 이유: 이 gate 가 이 시스템의 신뢰 기반이다. 프롬프트 지시("출처를
밝히세요")는 지켜지지 않아도 출력만 봐서는 드러나지 않지만, 집합 연산은 지켜진다 —
**단, 그 집합 연산이 실제로 돌 때만.** gate 가 통과만 시키면 gate 가 아니다.

특히 두 가지를 고정한다:
  1. **실재하는 문단 ID + 지어낸 인용문**이 폐기되는가. 가장 위험한 조합이며, ID 만
     대조하는 구현은 이것을 통과시킨다.
  2. **"처음부터 인용 0건"과 "폐기 후 0건"이 다르게 처리되는가.** 같게 처리하면
     "새 의무가 걸리는데 담을 조항이 없다"(골든셋 case-013)가 날조와 같은 취급을 받는다.
"""

from __future__ import annotations

from regchange.guards.citations import (
    DiscardReason,
    GateStatus,
    enforce_citations,
)
from regchange.prompts.obligation import (
    Citation,
    ExtractionStatus,
    Obligation,
    ObligationExtraction,
    ObligationType,
    SuggestedAction,
)

PARAGRAPH_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
PARAGRAPH_TEXT = (
    "① 정보보호부장은 침해사고를 인지한 즉시 과학기술정보통신부장관 또는\n"
    "한국인터넷진흥원에 신고한다.\n"
    "② 신고 사실을 정보보호 최고책임자에게 보고한다."
)
RETRIEVED = {PARAGRAPH_ID: PARAGRAPH_TEXT}
SCOPE = ("ISP-PROC-002 v2.4",)


def _extraction(
    *citations: Citation, status: ExtractionStatus = ExtractionStatus.OK
) -> ObligationExtraction:
    return ObligationExtraction(
        status=status,
        obligations=(
            Obligation(
                obligation_type=ObligationType.STRENGTHENED,
                summary="신고 기한이 즉시에서 24시간 이내로 명문화됐다",
                source_span="제48조의3제1항",
                citations=tuple(citations),
            ),
        ),
        reason="",
        suggested_action=SuggestedAction.NONE,
    )


def test_real_id_and_real_quote_passes() -> None:
    """실재하는 ID + 실재하는 인용문은 통과한다. gate 가 정상 근거를 막으면 쓸모가 없다."""
    result = enforce_citations(
        _extraction(Citation(PARAGRAPH_ID, "침해사고를 인지한 즉시", 0, 12)),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.status is GateStatus.OK
    assert result.citation_count == 1
    assert not result.discarded


def test_quote_spanning_line_break_passes() -> None:
    """줄바꿈만 다른 인용은 폐기하지 않는다. gate 가 서식이 아니라 실재를 잡아야 한다."""
    result = enforce_citations(
        _extraction(
            Citation(PARAGRAPH_ID, "과학기술정보통신부장관 또는 한국인터넷진흥원에 신고한다", 0, 30)
        ),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.status is GateStatus.OK


def test_unretrieved_id_is_discarded() -> None:
    """검색되지 않은 문단 ID 는 지어낸 출처다. 폐기하고 주장을 제거한다."""
    result = enforce_citations(
        _extraction(Citation(OTHER_ID, "침해사고를 인지한 즉시", 0, 12)),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert [d.reason for d in result.discarded] == [DiscardReason.NOT_RETRIEVED]
    assert len(result.removed) == 1
    assert not result.supported


def test_real_id_with_invented_quote_is_discarded() -> None:
    """**가장 위험한 조합** — 실재 ID 에 지어낸 인용문. ID 만 대조하면 통과한다."""
    result = enforce_citations(
        _extraction(Citation(PARAGRAPH_ID, "24시간 이내에 신고한다", 0, 12)),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert [d.reason for d in result.discarded] == [DiscardReason.QUOTE_NOT_FOUND]


def test_empty_quote_is_discarded() -> None:
    """빈 인용문은 근거가 아니다."""
    result = enforce_citations(
        _extraction(Citation(PARAGRAPH_ID, "   ", 0, 0)),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert [d.reason for d in result.discarded] == [DiscardReason.EMPTY_QUOTE]


def test_partial_discard_keeps_surviving_citation() -> None:
    """인용 일부만 폐기되면 주장은 남고 통과한 인용만 붙는다."""
    result = enforce_citations(
        _extraction(
            Citation(PARAGRAPH_ID, "침해사고를 인지한 즉시", 0, 12),
            Citation(OTHER_ID, "존재하지 않는 문단", 0, 9),
        ),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.status is GateStatus.OK
    assert result.citation_count == 1
    assert len(result.discarded) == 1
    assert not result.removed


def test_no_citations_from_the_start_keeps_the_claim() -> None:
    """처음부터 인용 0건은 날조가 아니다. 주장을 유지하되 근거 없음으로 분류한다.

    골든셋 case-013 — "새 의무가 우리에게 걸리는데 담을 조항이 없다"가 정답인 경우다.
    이것을 제거하면 담당자가 새 의무를 영영 보지 못한다.
    """
    result = enforce_citations(
        _extraction(status=ExtractionStatus.INSUFFICIENT_EVIDENCE),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert len(result.unsupported) == 1
    assert not result.removed, "제거된 주장으로 분류되면 안 된다"
    assert not result.discarded


def test_empty_retrieval_discards_everything() -> None:
    """검색 0건이면 모든 인용이 폐기된다. '영향 없음'이 아니라 '모른다'로 끝난다."""
    result = enforce_citations(
        _extraction(Citation(PARAGRAPH_ID, "침해사고를 인지한 즉시", 0, 12)),
        retrieved={},
        searched_scope=(),
    )
    assert result.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert result.citation_count == 0


def test_model_status_does_not_override_the_gate() -> None:
    """모델이 OK 라고 말해도 근거가 전부 폐기되면 충분하지 않다. 판정은 코드가 한다."""
    result = enforce_citations(
        _extraction(Citation(OTHER_ID, "지어낸 근거", 0, 6), status=ExtractionStatus.OK),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.status is GateStatus.INSUFFICIENT_EVIDENCE


def test_searched_scope_is_carried_to_the_result() -> None:
    """`INSUFFICIENT_EVIDENCE` 는 '어디까지 찾아봤는가'를 함께 내야 한다 (3단계 §6)."""
    result = enforce_citations(
        _extraction(Citation(OTHER_ID, "지어낸 근거", 0, 6)),
        retrieved=RETRIEVED,
        searched_scope=SCOPE,
    )
    assert result.searched_scope == SCOPE
