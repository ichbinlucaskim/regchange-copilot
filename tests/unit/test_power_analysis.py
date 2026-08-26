"""검정력 계산식이 원문(Miller 2024, arXiv:2411.00640)과 같은 값을 내는지 고정한다.

이 테스트가 존재하는 이유: `docs/23-metrics-summary.md` §3 이 「KURE 채택은 유의하지
않다」와 「δ=0.05 를 잡으려면 79건이 필요하다」를 이 함수들의 출력으로 주장한다.
식을 잘못 옮겨 적으면 그 주장이 조용히 틀리고, 틀린 방향이 어느 쪽인지도 알 수 없다.

**원문의 예제 계산(n ≈ 969)을 앵커로 쓴다.** 우리 데이터로 만든 기대값이 아니라 논문이
스스로 낸 수이므로, 이 값이 재현되면 상수(z 값 두 개)와 식의 형태가 함께 확인된다.
"""

from __future__ import annotations

import math

import pytest
from evals.runners.power_analysis import (
    Paired,
    clopper_pearson,
    mcnemar_exact,
    mde,
    sample_size,
    se_clt,
    se_clustered,
    se_paired_clustered,
)


def test_sample_size_reproduces_paper_example() -> None:
    # 원문 §5: σ²_A = σ²_B = 0, ω² = 1/9, δ = 0.03, alpha = 0.05, beta = 0.20 → n ≈ 969
    assert math.ceil(sample_size(1 / 9, 0.03)) == 969


def test_mde_inverts_sample_size() -> None:
    # 식 (10)은 식 (9)의 역이다. 왕복하면 같은 값이 나와야 한다.
    variance_term = 0.025
    n = sample_size(variance_term, 0.05)
    assert mde(variance_term, round(n)) == pytest.approx(0.05, abs=1e-3)


def test_se_clustered_equals_clt_when_every_cluster_is_singleton() -> None:
    # 클러스터 내부 교차항이 없으면 식 (4)는 식 (1)로 되돌아간다.
    scores = [1.0, 0.0, 1.0, 1.0, 0.0]
    clusters = ["a", "b", "c", "d", "e"]
    assert se_clustered(scores, clusters) == pytest.approx(se_clt(scores))


def test_se_clustered_grows_when_a_cluster_moves_together() -> None:
    # 같은 클러스터의 점수가 함께 움직이면 독립 표집 가정이 과소평가한다.
    scores = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    together = ["a", "a", "a", "b", "b", "b"]
    apart = ["a", "b", "c", "a", "b", "c"]
    assert se_clustered(scores, together) > se_clt(scores)
    assert se_clustered(scores, apart) < se_clustered(scores, together)


def test_se_paired_clustered_matches_hand_computation() -> None:
    # 식 (8): (1/n)·sqrt( Σ_c (Σ_i 잔차)² ). 잔차합이 상쇄되는 클러스터는 기여가 0이다.
    diffs = [1.0, -1.0, 1.0, -1.0]
    clusters = ["a", "a", "b", "b"]
    assert se_paired_clustered(diffs, clusters) == pytest.approx(0.0)


def test_mcnemar_exact_known_values() -> None:
    assert mcnemar_exact(0, 0) == 1.0  # 불일치 없음 — 「차이 없음」이 아니다
    assert mcnemar_exact(1, 0) == pytest.approx(1.0)
    assert mcnemar_exact(3, 0) == pytest.approx(0.25)
    assert mcnemar_exact(5, 0) == pytest.approx(0.0625)
    assert mcnemar_exact(3, 3) == pytest.approx(1.0)


def test_mcnemar_is_more_conservative_than_normal_approximation() -> None:
    # de-anchored 대조가 실제로 이 자리에 있었다: 정규근사는 z=1.96 을 넘고
    # 정확검정은 p=0.25 다. 두 방법이 갈린다는 사실 자체가 결과다.
    diffs = [1.0, 1.0, 1.0] + [0.0] * 7
    mean = sum(diffs) / len(diffs)
    variance = sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
    se = math.sqrt(variance / len(diffs))
    assert mean / se > 1.959964
    assert mcnemar_exact(3, 0) > 0.05


def test_paired_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="길이가 어긋난다"):
        Paired(
            name="x",
            unit="테스트",
            keys=("a", "b"),
            clusters=("c1", "c1"),
            a=(1.0, 0.0),
            b=(1.0,),
            label_a="A",
            label_b="B",
        )


def test_paired_rejects_empty() -> None:
    with pytest.raises(ValueError, match="질문이 0개"):
        Paired(
            name="x",
            unit="테스트",
            keys=(),
            clusters=(),
            a=(),
            b=(),
            label_a="A",
            label_b="B",
        )


def test_sample_size_rejects_nonpositive_delta() -> None:
    # δ=0 이면 필요 표본이 무한대다. 무한대를 돌려주면 호출부가 그것을 수로 쓴다.
    with pytest.raises(ValueError, match="양수"):
        sample_size(0.1, 0.0)


def test_clopper_pearson_matches_published_values() -> None:
    # 표준 이항 신뢰구간표의 값이다. n=20 에서 0/20 의 상한이 이 측정의 근거가 된다.
    assert clopper_pearson(0, 20) == pytest.approx((0.0, 0.1684), abs=1e-3)
    assert clopper_pearson(1, 20) == pytest.approx((0.0013, 0.2487), abs=1e-3)
    assert clopper_pearson(2, 20) == pytest.approx((0.0123, 0.3170), abs=1e-3)
    assert clopper_pearson(20, 20) == pytest.approx((0.8316, 1.0), abs=1e-3)


def test_clopper_pearson_is_wider_than_the_normal_approximation_at_zero() -> None:
    # 관측 0 에서 정규근사는 폭 0 인 구간을 준다. 그래서 정확검정을 쓴다.
    _, high = clopper_pearson(0, 20)
    assert high > 0.16


def test_clopper_pearson_refuses_zero_denominator() -> None:
    # 분모 0 에 구간을 주지 않는다 — `discard_rate: null` 과 같은 규칙이다.
    with pytest.raises(ValueError, match="분모가 0"):
        clopper_pearson(0, 0)


def test_clopper_pearson_refuses_impossible_counts() -> None:
    with pytest.raises(ValueError, match="범위를 벗어났다"):
        clopper_pearson(21, 20)
