"""RRF 가 점수 척도를 보지 않고 순위만으로 결합하는지 검사한다.

이 테스트가 존재하는 이유: 하이브리드를 쓸지는 측정으로 정하지만, 결합 방식 자체는
"근거 없는 상수를 만들지 않는다"는 규약(CLAUDE.md §4)에 걸려 있다. 가중합으로
되돌아가면 정규화 방식과 가중치라는 매직 넘버 두 개가 조용히 들어온다.
"""

from __future__ import annotations

from regchange.retrieval.fusion import reciprocal_rank_fusion


def test_single_ranking_is_preserved() -> None:
    """목록이 하나면 그 순위를 그대로 보존한다 (단조 변환)."""
    fused = reciprocal_rank_fusion([["a", "b", "c"]], limit=3)
    assert [identifier for identifier, _ in fused] == ["a", "b", "c"]


def test_agreement_beats_single_list_top() -> None:
    """두 목록이 모두 지목한 항목이, 한쪽에서만 1위인 항목보다 위로 온다."""
    fused = reciprocal_rank_fusion([["x", "b"], ["y", "b"]], limit=3)
    assert fused[0][0] == "b"


def test_empty_inputs_yield_empty_result() -> None:
    """모든 목록이 비면 빈 결과다."""
    assert reciprocal_rank_fusion([[], []], limit=5) == ()


def test_duplicate_in_one_ranking_counts_once() -> None:
    """한 목록에 같은 식별자가 두 번 있어도 점수를 두 배로 키우지 않는다."""
    doubled = reciprocal_rank_fusion([["a", "a"]], limit=1)
    single = reciprocal_rank_fusion([["a"]], limit=1)
    assert doubled[0][1] == single[0][1]


def test_ties_are_ordered_by_identifier() -> None:
    """동점은 식별자 순으로 안정 정렬한다. 실행마다 순위가 흔들리면 재현이 깨진다."""
    fused = reciprocal_rank_fusion([["b"], ["a"]], limit=2)
    assert [identifier for identifier, _ in fused] == ["a", "b"]


def test_score_scale_is_ignored() -> None:
    """RRF 는 점수를 아예 받지 않는다 — 척도 문제와 정규화 상수가 성립할 수 없다."""
    # 입력 타입이 순위 목록이므로, 큰 점수를 가진 쪽을 우대할 방법이 존재하지 않는다.
    left = reciprocal_rank_fusion([["a", "b"], ["b", "a"]], limit=2)
    right = reciprocal_rank_fusion([["b", "a"], ["a", "b"]], limit=2)
    assert {identifier for identifier, _ in left} == {identifier for identifier, _ in right}
    assert left[0][1] == right[0][1]
