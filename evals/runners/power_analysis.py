"""검정력 분석 — **지금까지의 대조가 유의한가, 유의하려면 몇 건이 필요한가**.

    uv run --group eval python -m evals.runners.power_analysis

무엇을 재는가:
    이 저장소는 지금까지 세 번, **관측된 차이를 근거로 노브를 확정했다.**

      | 결정 | 근거로 삼은 차이 | 표본 |
      |---|---|---|
      | KURE-v1 채택 (ADR-015) | 재현율@10 0.7667 vs 0.7167 = **0.05** | IMPACT 10 케이스 |
      | de-anchored 기각 (ADR-013) | IMPACT 적중 9 vs 6 = **3건** | IMPACT 10 케이스 |
      | `MAX_REVISIONS=0` (ADR-013) | IMPACT 적중 9 vs 8 = **1건** | IMPACT 10 케이스 |

    `docs/15-variability-results.md` §5.2 는 이 셋을 「근거 차이 > 관측 편차」로 유지
    판정했다. **그 부등식은 잡음의 크기만 본다.** 표본이 작으면 차이가 편차보다 커도
    우연일 수 있고, 그 질문에 답하는 것은 편차가 아니라 **표준오차**다.

    이 러너는 Miller (2024) 「Adding Error Bars to Evals」(arXiv:2411.00640) 의
    식을 그대로 구현해 세 대조의 **쌍체 표준오차·신뢰구간·필요 표본 수**를 낸다.

    **LLM 을 부르지 않는다. 비용 0.** 이미 있는 결과 파일만 다시 읽는다.

──────────────────────────────────────────────────────────────────────────────
구현한 식 — 전부 원문에서 가져왔다
──────────────────────────────────────────────────────────────────────────────

    (1)  SE_CLT          = sqrt( Var(s) / n )
    (4)  SE_clustered    = [ SE_CLT² + (1/n²)·Σ_c Σ_i Σ_{j≠i} (s_ic-s̄)(s_jc-s̄) ]^½
    (7)  SE_paired       = sqrt( Var(s_A - s_B) / n )       ← 분모 n-1 (Bessel)
    (8)  SE_paired_clust = (1/n)·[ Σ_c Σ_i Σ_j (d_ic-d̄)(d_jc-d̄) ]^½   ← Bessel 없음
    (9)  n               = (z_{alpha/2}+z_beta)² (ω² + σ²_A/K_A + σ²_B/K_B) / δ²
    (10) δ               = (z_{alpha/2}+z_beta) · sqrt( (ω² + σ²_A/K_A + σ²_B/K_B) / n )

    (7)과 (8)의 분모가 다른 것은 **원문 그대로다.** 맞춰 쓰지 않는다 — 맞추면 우리가
    구현한 것이 원문의 식이 아니게 되고, 두 값의 비(설계효과)만 쓰는 이 러너에서는
    n=10 에서 약 5% 차이가 결론을 바꾸지 않는다.

    **클러스터 보정은 부록 C 를 따른다.** 부록 C 는 (9)의 `ω² + σ²/K` 자리에
    `n · Var_clustered(μ̂_{A-B})` 를 넣는 것과 같다 — Var_clustered 의 정의가 곧
    (8)의 제곱이기 때문이다. 별도 공식이 아니라 같은 식의 입력 교체다.

──────────────────────────────────────────────────────────────────────────────
ω² 를 어떻게 얻는가 — **관측 분산에서 잡음을 빼야 한다**
──────────────────────────────────────────────────────────────────────────────

원문의 ω² 는 **잡음이 없는** 점수 차의 분산이다. 우리가 관측하는 것은 잡음이 섞인
차이이며 관계는 이렇다.

    Var(관측된 차이) = ω² + σ²_A/K_A + σ²_B/K_B

따라서 `ω² = Var(관측 차이) - σ²_A - σ²_B` (K=1). 우리는 두 항을 다르게 얻는다.

    검색 경로 : σ² = 0. **측정값이다** — `docs/15` §2 가 3회 완전 일치(편차 0)를
                기록했고, 그 경로에는 샘플링이 없다
    LLM 경로  : σ² 를 `docs/15` 의 3회 반복에서 케이스별 표본분산으로 추정하고
                평균한다. **σ²_B(대조군)는 1회만 돌았으므로 같은 값을 쓴다** —
                두 설정의 잡음이 같다는 가정이며, 틀리면 필요 표본 수가 과소평가된다

**뺀 값이 음수면 0 으로 자른다.** 잡음 추정이 관측 분산보다 커진 경우이며, 그 사실을
결과에 `omega2_clipped` 로 남긴다 — 조용히 0 으로 두면 「분산이 없었다」로 읽힌다.

──────────────────────────────────────────────────────────────────────────────
이 계산의 한계 — **축소하지 않는다**
──────────────────────────────────────────────────────────────────────────────

1. **케이스가 독립이 아니다.** 15건 골든셋의 IMPACT 10 케이스는 원천이 3개뿐이고
   (285199 5건 · 283839 4건 · 283503 1건), 42건에서도 IMPACT 20 중 15건이 두
   원천이다 (`docs/21` §4). CLT 는 독립 표집을 가정하므로 (1)·(7)을 그대로 쓰면
   표준오차가 과소평가된다. 그래서 **클러스터 보정판을 함께 낸다.**

2. **이항 점수 + n=10 에서 정규근사가 약하다.** 적중 여부는 0/1 이고 불일치 쌍이
   한쪽으로 몰려 있다. 그래서 **McNemar 정확검정을 함께 낸다** — 두 p 값이 크게
   갈리면 갈린다는 사실 자체가 결과다.

3. **분산 추정치가 같은 10건에서 나왔다.** ω² 도 σ² 도 표본 10(반복 3)에서 뽑은
   값이라 그 자체가 크게 흔들린다. 필요 표본 수는 **자릿수 감각**이지 확정값이 아니다.

4. **여기서 하는 것은 사후 계산이다.** 결정은 이미 내려졌고 이 러너는 그 결정이
   어떤 정밀도 위에 있었는지를 잰다. **결과가 「유의하지 않다」여도 결정을 뒤집지
   않는다** — 뒤집으려면 그것도 근거가 필요하고, 「유의하지 않다」는 근거가 아니다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"

ALPHA = 0.05
"""1종 오류율. **관행** — Fisher 이후의 표준값이며 우리 데이터로 고른 것이 아니다.
바꾸면 필요 표본 수가 함께 움직인다."""

POWER = 0.80
"""검정력 1-beta. **관행** — Cohen 이 제안한 관례값. 우리 데이터로 고른 것이 아니다."""

Z_ALPHA_HALF = 1.959964
"""표준정규의 (1-alpha/2) 분위, alpha=0.05. `scipy` 를 끌어오지 않으려고 상수로 둔다 —
이 러너가 쓰는 유일한 분위값 두 개이며, 값이 바뀔 일이 없다."""

Z_BETA = 0.841621
"""표준정규의 (1-beta) 분위, beta=0.20."""

TARGET_DELTAS = (0.05, 0.10, 0.20)
"""필요 표본 수를 낼 최소검출효과(MDE) 후보.

0.05 는 **지시가 지정한 값**(「0.05 차이를 유의하게 잡으려면 몇 건이 필요한가」)이고,
0.10 과 0.20 은 그 값이 도달 불가능할 때 **무엇이라면 가능한가**를 보이기 위한
대조점이다. 셋 다 절대 차이이며 비율이 아니다."""

CURRENT_SET_SIZES = (10, 20, 42, 80)
"""MDE 를 계산할 표본 크기.

10 = 15건 골든셋의 IMPACT 케이스 수, 20 = 42건 골든셋의 IMPACT 케이스 수,
42 = 42건 전체, 80 = `docs/15` §8 이 「다음 측정에서 볼 것」으로 적어 둔 규모.
**우리가 실제로 가졌거나 가질 수 있는 수만 넣는다.**"""

BISECTION_STEPS = 200
"""Clopper-Pearson 구간을 이분법으로 뒤집을 때의 반복 수.

**설계값이다.** 구간 폭 1 을 200 번 반으로 접으면 배정밀도 부동소수의 표현 한계보다
훨씬 아래로 내려가므로, 이 값은 정밀도를 정하는 노브가 아니라 **수렴을 보장하는
상한**이다. 낮추면 자릿수를 잃고, 올려도 얻는 것이 없다."""

VARIABILITY_RUNS = (
    "impact-claude-sonnet-5-20260822T205741Z.json",
    "impact-claude-sonnet-5-20260822T212517Z.json",
    "impact-claude-sonnet-5-20260822T215241Z.json",
)
"""`docs/15` 의 3회 반복 실행. σ² 추정의 유일한 재료다."""

logger = logging.getLogger("power_analysis")


@dataclass(frozen=True, slots=True)
class Paired:
    """한 대조의 쌍체 자료.

    목적:
        두 설정을 같은 질문 집합에서 잰 점수와 그 질문이 속한 클러스터를 한 값으로
        담는다.

    구현 이유:
        점수와 클러스터를 따로 넘기면 순서가 어긋나도 계산이 되어 버린다. 한 객체에
        묶고 생성 시점에 길이를 맞춘다.

    트레이드오프:
        질문 식별자를 문자열로 들고 다니므로 케이스와 항목을 같은 타입으로 다룬다.
        단위를 타입으로 가르지 않은 대신, 어느 단위로 쟀는지를 `unit` 에 적는다.

    엣지 케이스:
        - 길이가 다른 두 점수 목록: `__post_init__` 이 `ValueError` 를 던진다.
          짧은 쪽에 맞추면 조용히 다른 질문을 비교하게 된다.
        - 클러스터가 전부 다른 경우: 클러스터 보정이 무보정과 같아진다. 오류가
          아니라 「클러스터 구조가 없다」이며 그대로 계산된다.
    """

    name: str
    unit: str
    keys: tuple[str, ...]
    clusters: tuple[str, ...]
    a: tuple[float, ...]
    b: tuple[float, ...]
    label_a: str
    label_b: str

    def __post_init__(self) -> None:
        """네 목록의 길이가 같고 비어 있지 않은지 확인한다."""
        lengths = {len(self.keys), len(self.clusters), len(self.a), len(self.b)}
        if len(lengths) != 1:
            msg = f"{self.name}: 쌍체 자료의 길이가 어긋난다 {lengths}"
            raise ValueError(msg)
        if not self.keys:
            msg = f"{self.name}: 질문이 0개다"
            raise ValueError(msg)

    @property
    def diffs(self) -> tuple[float, ...]:
        """질문별 점수 차 (A - B)."""
        return tuple(x - y for x, y in zip(self.a, self.b, strict=True))


def se_clt(scores: Sequence[float]) -> float:
    """식 (1) — 독립 표집을 가정한 평균의 표준오차."""
    n = len(scores)
    if n < 2:
        return float("nan")
    return math.sqrt(statistics.variance(scores) / n)


def se_clustered(scores: Sequence[float], clusters: Sequence[str]) -> float:
    """식 (4) — 질문이 무리지어 뽑혔을 때의 표준오차.

    목적:
        같은 원천에서 여러 케이스를 뽑은 우리 골든셋에서 표준오차가 얼마나
        과소평가되는지를 낸다.

    구현 이유:
        원문의 삼중합을 그대로 옮긴다. 클러스터 내부의 교차항(i≠j)만 더하는
        형태이며, 클러스터 안이 완전상관이면 클러스터 하나가 관측 하나가 되고
        무상관이면 (1)과 같아진다.

    트레이드오프:
        교차항 합이 음수면 클러스터 보정 표준오차가 무보정보다 **작아질 수 있다.**
        음수를 0 으로 자르지 않는다 — 자르면 「보정은 항상 커진다」는 없는 성질을
        코드가 주장하게 된다. 근호 안이 음수가 되면 `nan` 을 돌려준다.

    엣지 케이스:
        - 클러스터가 전부 크기 1: 교차항이 없어 (1)과 정확히 같다.
        - 근호 안이 음수: `nan`. 호출부가 그 사실을 결과에 남긴다.
    """
    n = len(scores)
    if n < 2:
        return float("nan")
    mean = statistics.fmean(scores)
    groups: dict[str, list[float]] = {}
    for score, cluster in zip(scores, clusters, strict=True):
        groups.setdefault(cluster, []).append(score - mean)
    cross = 0.0
    for residuals in groups.values():
        total = sum(residuals)
        cross += total * total - sum(r * r for r in residuals)
    inside = se_clt(scores) ** 2 + cross / (n * n)
    if inside < 0:
        return float("nan")
    return math.sqrt(inside)


def se_paired(diffs: Sequence[float]) -> float:
    """식 (7) — 쌍체 차이의 표준오차."""
    return se_clt(diffs)


def se_paired_clustered(diffs: Sequence[float], clusters: Sequence[str]) -> float:
    """식 (8) — 클러스터를 감안한 쌍체 차이의 표준오차.

    목적:
        같은 원천의 케이스들이 함께 맞거나 함께 틀리는 구조를 표준오차에 반영한다.

    구현 이유:
        원문의 삼중합은 i=j 를 포함하므로 클러스터별 잔차합의 제곱을 더하는 것과
        같다. 그 형태로 구현한다 — 삼중 루프를 그대로 쓰면 n² 이 되고 결과는 같다.

    트레이드오프:
        원문 식에 Bessel 보정이 없다(식 (7)에는 있다). 원문대로 둔다. n=10 에서
        약 5% 차이이며, 이 러너가 쓰는 것은 두 값의 비다.

    엣지 케이스:
        - 클러스터가 전부 크기 1: 잔차합의 제곱 합이 잔차제곱합과 같아지고,
          결과는 Bessel 보정만큼만 (7)과 다르다.
    """
    n = len(diffs)
    if n < 2:
        return float("nan")
    mean = statistics.fmean(diffs)
    groups: dict[str, list[float]] = {}
    for diff, cluster in zip(diffs, clusters, strict=True):
        groups.setdefault(cluster, []).append(diff - mean)
    total = sum(sum(residuals) ** 2 for residuals in groups.values())
    return math.sqrt(total) / n


def sample_size(variance_term: float, delta: float) -> float:
    """식 (9) — 최소검출효과 `delta` 를 잡는 데 필요한 독립 질문 수.

    `variance_term` 은 `ω² + σ²_A/K_A + σ²_B/K_B` 이며, 클러스터 보정판은
    `n · SE_clustered²` 를 그 자리에 넣는다 (부록 C).
    """
    if delta <= 0:
        msg = "delta 는 양수여야 한다"
        raise ValueError(msg)
    return (Z_ALPHA_HALF + Z_BETA) ** 2 * variance_term / (delta * delta)


def mde(variance_term: float, n: int) -> float:
    """식 (10) — 표본 `n` 에서 잡을 수 있는 최소검출효과."""
    if n < 1:
        msg = "n 은 1 이상이어야 한다"
        raise ValueError(msg)
    return (Z_ALPHA_HALF + Z_BETA) * math.sqrt(variance_term / n)


def mcnemar_exact(b: int, c: int) -> float:
    """이항 쌍체 자료의 McNemar 정확검정 양측 p 값.

    목적:
        적중 여부처럼 0/1 인 점수에서 정규근사가 못 미더울 때의 대조값을 낸다.

    구현 이유:
        불일치 쌍이 `b + c` 개일 때 귀무가설은 「각 불일치가 5:5」이므로 이항분포
        B(b+c, 0.5) 의 양측 꼬리다. n=10 에 정규근사를 쓰면 p 가 낙관적으로 나오고,
        이 저장소는 그 낙관 위에 노브를 세 개 얹었다.

    트레이드오프:
        정확검정은 보수적이다(실제 1종 오류율이 alpha 보다 작다). 보수적인 쪽으로
        틀리는 것을 택했다 — 규제 도메인에서 「유의하다」를 과하게 말하지 않는다.

    엣지 케이스:
        - `b + c == 0`: 불일치가 없다. p = 1.0 이며 「차이가 없다」가 아니라
          「이 자료로는 아무것도 말할 수 없다」이다. 호출부가 구별해 적는다.
        - 양측 확률이 1 을 넘는 경우(대칭점에서 2배): 1.0 으로 자른다.
    """
    n = b + c
    if n == 0:
        return 1.0
    extreme = max(b, c)
    tail = sum(math.comb(n, k) for k in range(extreme, n + 1)) * 0.5**n
    return min(1.0, 2.0 * tail)


def clopper_pearson(successes: int, trials: int, alpha: float = ALPHA) -> tuple[float, float]:
    """이항 비율의 **정확(Clopper-Pearson) 신뢰구간**.

    목적:
        n 이 작고 관측이 0 에 붙어 있는 비율(F-6 판정 20건)의 구간을 낸다.

    구현 이유:
        정규근사도 Wilson 도 n=20 · 관측 0 에서는 실제 포함확률이 명목치에 못 미친다.
        Clopper-Pearson 은 이항 꼬리확률을 직접 뒤집으므로 **어떤 n 에서도 명목
        수준 이상을 보장한다.** 이 값이 「0건이 나와도 실제 비율은 얼마까지일 수
        있는가」라는 문장의 근거가 되므로, 보수적인 쪽으로 틀리는 구간을 쓴다.

        `scipy` 를 끌어오지 않고 이항 CDF 를 직접 더해 이분법으로 뒤집는다.
        n 이 수십 규모라 `math.comb` 합산이 정확하고 빠르다.

    트레이드오프:
        보수적이라 구간이 Wilson 보다 넓다(0/20 에서 상한 0.168 vs 0.161).
        구간이 좁아 보이는 것보다 **포함확률을 지키는 쪽**을 택했다 — 이 구간은
        「F-6 이 없다고 쓰지 않기 위한」 상한이므로 좁게 잡으면 목적을 잃는다.

    엣지 케이스:
        - `successes == 0`: 하한은 정확히 0.0 이다. 이분법을 돌리지 않는다.
        - `successes == trials`: 상한은 정확히 1.0 이다.
        - `trials <= 0`: `ValueError`. **분모 0 에 구간을 주지 않는다** — 이
          저장소가 `discard_rate: null` 로 남기기로 한 것과 같은 규칙이다.
    """
    if trials <= 0:
        msg = "분모가 0 이면 신뢰구간이 정의되지 않는다"
        raise ValueError(msg)
    if not 0 <= successes <= trials:
        msg = f"성공 수가 범위를 벗어났다: {successes}/{trials}"
        raise ValueError(msg)

    def cdf_upper(p: float) -> float:
        """P(X >= successes | p)."""
        return sum(
            math.comb(trials, k) * p**k * (1 - p) ** (trials - k)
            for k in range(successes, trials + 1)
        )

    def cdf_lower(p: float) -> float:
        """P(X <= successes | p)."""
        return sum(
            math.comb(trials, k) * p**k * (1 - p) ** (trials - k) for k in range(successes + 1)
        )

    half = alpha / 2

    def bisect(target: Any, level: float, increasing: bool) -> float:
        low, high = 0.0, 1.0
        for _ in range(BISECTION_STEPS):
            mid = (low + high) / 2
            if (target(mid) < level) is increasing:
                low = mid
            else:
                high = mid
        return (low + high) / 2

    lower = 0.0 if successes == 0 else bisect(cdf_upper, half, increasing=True)
    upper = 1.0 if successes == trials else bisect(cdf_lower, half, increasing=False)
    return lower, upper


def analyze(paired: Paired, sigma2_a: float, sigma2_b: float) -> dict[str, Any]:
    """한 대조의 표준오차·신뢰구간·필요 표본 수를 낸다.

    목적:
        식 (1)·(4)·(7)·(8)·(9)·(10)과 McNemar 정확검정을 한 대조에 모두 적용해
        한 사전으로 돌려준다.

    구현 이유:
        보정 전후와 근사/정확을 **함께** 낸다. 하나만 내면 「유의하다」와
        「유의하지 않다」 중 편한 쪽이 남는다.

    트레이드오프:
        출력이 넓어 읽기 어렵다. 대신 어느 값이 어느 식에서 나왔는지 키 이름으로
        추적된다 — 문서가 인용할 때 그 이름을 그대로 쓴다.

    엣지 케이스:
        - `ω²` 가 음수로 계산됨: 0 으로 자르고 `omega2_clipped=True` 를 남긴다.
        - 이항이 아닌 점수: McNemar 는 계산하지 않고 `null` 을 남긴다.
          0 과 부재를 구별한다.
    """
    diffs = paired.diffs
    n = len(diffs)
    mean_diff = statistics.fmean(diffs)
    se = se_paired(diffs)
    se_cl = se_paired_clustered(diffs, paired.clusters)
    observed_var = statistics.variance(diffs)

    omega2_raw = observed_var - sigma2_a - sigma2_b
    omega2 = max(0.0, omega2_raw)
    variance_term = omega2 + sigma2_a + sigma2_b
    # 클러스터 보정 분산항 (부록 C): n · Var_clustered(μ̂)
    variance_term_clustered = n * se_cl * se_cl if math.isfinite(se_cl) else float("nan")

    binary = all(value in (0.0, 1.0) for value in paired.a + paired.b)
    mcnemar: float | None = None
    discordant: dict[str, int] | None = None
    if binary:
        b_count = sum(1 for d in diffs if d > 0)
        c_count = sum(1 for d in diffs if d < 0)
        discordant = {"a_only": b_count, "b_only": c_count}
        mcnemar = mcnemar_exact(b_count, c_count)

    return {
        "name": paired.name,
        "unit": paired.unit,
        "label_a": paired.label_a,
        "label_b": paired.label_b,
        "n": n,
        "clusters": len(set(paired.clusters)),
        "cluster_sizes": sorted(
            (paired.clusters.count(c) for c in dict.fromkeys(paired.clusters)), reverse=True
        ),
        "mean_a": round(statistics.fmean(paired.a), 4),
        "mean_b": round(statistics.fmean(paired.b), 4),
        "mean_diff": round(mean_diff, 4),
        "nonzero_diffs": sum(1 for d in diffs if d != 0),
        "se_paired": round(se, 4),
        "se_paired_clustered": round(se_cl, 4) if math.isfinite(se_cl) else None,
        "design_effect": round((se_cl / se) ** 2, 3) if math.isfinite(se_cl) and se > 0 else None,
        "ci95": [round(mean_diff - Z_ALPHA_HALF * se, 4), round(mean_diff + Z_ALPHA_HALF * se, 4)],
        "ci95_clustered": (
            [
                round(mean_diff - Z_ALPHA_HALF * se_cl, 4),
                round(mean_diff + Z_ALPHA_HALF * se_cl, 4),
            ]
            if math.isfinite(se_cl)
            else None
        ),
        "z": round(mean_diff / se, 3) if se > 0 else None,
        "significant_normal": bool(se > 0 and abs(mean_diff / se) >= Z_ALPHA_HALF),
        "mcnemar_exact_p": round(mcnemar, 4) if mcnemar is not None else None,
        "mcnemar_discordant": discordant,
        "sigma2_a": round(sigma2_a, 5),
        "sigma2_b": round(sigma2_b, 5),
        "observed_var": round(observed_var, 5),
        "omega2": round(omega2, 5),
        "omega2_clipped": omega2_raw < 0,
        "required_n": {
            f"{delta:.2f}": math.ceil(sample_size(variance_term, delta)) for delta in TARGET_DELTAS
        },
        "required_n_clustered": (
            {
                f"{delta:.2f}": math.ceil(sample_size(variance_term_clustered, delta))
                for delta in TARGET_DELTAS
            }
            if math.isfinite(variance_term_clustered)
            else None
        ),
        "mde": {str(size): round(mde(variance_term, size), 4) for size in CURRENT_SET_SIZES},
        "mde_clustered": (
            {str(size): round(mde(variance_term_clustered, size), 4) for size in CURRENT_SET_SIZES}
            if math.isfinite(variance_term_clustered)
            else None
        ),
    }


def load_cases(name: str) -> dict[str, dict[str, Any]]:
    """결과 파일의 케이스를 `case_id` 로 색인한다."""
    data: dict[str, Any] = json.loads((RESULTS_DIR / name).read_text(encoding="utf-8"))
    return {str(case["case_id"]): case for case in data["cases"]}


def load_clusters() -> dict[str, str]:
    """케이스별 원천 MST. 클러스터 변수이며 골든셋 정의에서 온다."""
    clusters: dict[str, str] = {}
    for path in sorted(GOLDEN_DIR.glob("case-*.yaml")):
        case: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        clusters[str(case["id"])] = str(case["source"]["mst"])
    return clusters


def hit_score(case: dict[str, Any]) -> float:
    """케이스 적중 여부 (정답 문단을 하나라도 인용했는가)."""
    return 1.0 if case["hit"] else 0.0


def recall_score(case: dict[str, Any]) -> float:
    """케이스별 정답 문단 재현율. 정답이 0개인 케이스는 호출부가 걸러야 한다."""
    return len(case["hit"]) / len(case["expected"])


def sigma2_from_repeats(
    runs: Sequence[dict[str, dict[str, Any]]],
    keys: Sequence[str],
    score: Callable[[dict[str, Any]], float],
) -> float:
    """반복 실행에서 케이스별 조건부 분산을 추정해 평균한다 (원문의 σ²).

    목적:
        같은 설정을 3회 돌린 자료로 「같은 질문을 다시 물으면 얼마나 흔들리는가」를
        낸다. 식 (9)가 요구하는 σ² 가 이것이다.

    구현 이유:
        `docs/15` 가 이미 3회 반복을 돌려 두었다. 그 자료가 검정력 계산이 요구하는
        바로 그 양이며, 다시 돌리면 돈이 든다.

    트레이드오프:
        K=3 은 분산 추정에 매우 작다. **0 이 나와도 「흔들리지 않는다」가 아니다** —
        3회 같은 값이 나올 확률은 흔들림이 작지 않아도 낮지 않다. 이 사실은 결과
        문서가 적는다.

    엣지 케이스:
        - 실행이 2개 미만: `nan`. 분산을 정의할 수 없다.
        - 케이스가 어느 실행에 없음: `KeyError` 를 그대로 올린다. 빠진 케이스를
          건너뛰면 분모가 조용히 달라진다.
    """
    if len(runs) < 2:
        return float("nan")
    per_case = [statistics.variance([score(run[key]) for run in runs]) for key in keys]
    return statistics.fmean(per_case)


def build(results: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """세 대조를 만들어 분석한다.

    목적:
        ADR-015 · ADR-013 의 근거가 된 세 차이를 같은 방법으로 잰다.

    구현 이유:
        세 결정이 전부 IMPACT 10 케이스 위에 있으므로 질문 집합과 클러스터가 같다.
        같은 함수로 재야 「어느 것이 더 약한 근거인가」를 말할 수 있다.

    트레이드오프:
        검색 대조는 케이스 단위 재현율로, LLM 대조는 적중(이항)과 재현율 둘 다로
        잰다. 단위가 섞이지만 각 결정이 실제로 근거로 삼은 지표를 그대로 쓴다 —
        지금 단위를 통일하면 그때의 결정을 재는 것이 아니게 된다.

    엣지 케이스:
        - 결과 파일에 없는 케이스: `build` 가 교집합만 쓴다. 어느 케이스가 빠졌는지
          결과의 `keys` 로 확인된다.
    """
    clusters = load_clusters()
    anchored = results["anchored"]
    impact_keys = tuple(
        cid for cid, case in sorted(anchored.items()) if case["expected_outcome"] == "IMPACT"
    )
    repeats = [results[f"repeat{i}"] for i in range(len(VARIABILITY_RUNS))]

    sigma2_hit = sigma2_from_repeats(repeats, impact_keys, hit_score)
    sigma2_recall = sigma2_from_repeats(repeats, impact_keys, recall_score)

    kure = results["kure"]
    bge = results["bge"]
    retrieval_keys = tuple(cid for cid in impact_keys if kure[cid]["expected"])

    comparisons: list[tuple[Paired, float, float]] = [
        (
            Paired(
                name="EMBEDDING_KURE_vs_BGE",
                unit="케이스별 재현율@10 (VECTOR)",
                keys=retrieval_keys,
                clusters=tuple(clusters[cid] for cid in retrieval_keys),
                a=tuple(kure[cid]["recall@10"] for cid in retrieval_keys),
                b=tuple(bge[cid]["recall@10"] for cid in retrieval_keys),
                label_a="KURE-v1",
                label_b="bge-m3",
            ),
            0.0,
            0.0,
        ),
        (
            Paired(
                name="GROUNDING_anchored_vs_deanchored__hit",
                unit="케이스 적중 (0/1)",
                keys=impact_keys,
                clusters=tuple(clusters[cid] for cid in impact_keys),
                a=tuple(hit_score(anchored[cid]) for cid in impact_keys),
                b=tuple(hit_score(results["deanchored"][cid]) for cid in impact_keys),
                label_a="anchored",
                label_b="de-anchored",
            ),
            sigma2_hit,
            sigma2_hit,
        ),
        (
            Paired(
                name="GROUNDING_anchored_vs_deanchored__recall",
                unit="케이스별 정답 문단 재현율",
                keys=impact_keys,
                clusters=tuple(clusters[cid] for cid in impact_keys),
                a=tuple(recall_score(anchored[cid]) for cid in impact_keys),
                b=tuple(recall_score(results["deanchored"][cid]) for cid in impact_keys),
                label_a="anchored",
                label_b="de-anchored",
            ),
            sigma2_recall,
            sigma2_recall,
        ),
        (
            Paired(
                name="MAX_REVISIONS_1_vs_0__hit",
                unit="케이스 적중 (0/1)",
                keys=impact_keys,
                clusters=tuple(clusters[cid] for cid in impact_keys),
                a=tuple(hit_score(anchored[cid]) for cid in impact_keys),
                b=tuple(hit_score(results["repeat0"][cid]) for cid in impact_keys),
                label_a="MAX_REVISIONS=1",
                label_b="MAX_REVISIONS=0 (회차1)",
            ),
            sigma2_hit,
            sigma2_hit,
        ),
        (
            Paired(
                name="MAX_REVISIONS_1_vs_0__recall",
                unit="케이스별 정답 문단 재현율",
                keys=impact_keys,
                clusters=tuple(clusters[cid] for cid in impact_keys),
                a=tuple(recall_score(anchored[cid]) for cid in impact_keys),
                b=tuple(recall_score(results["repeat0"][cid]) for cid in impact_keys),
                label_a="MAX_REVISIONS=1",
                label_b="MAX_REVISIONS=0 (회차1)",
            ),
            sigma2_recall,
            sigma2_recall,
        ),
    ]
    return [analyze(paired, s_a, s_b) for paired, s_a, s_b in comparisons]


def load_retrieval(name: str, mode: str) -> dict[str, dict[str, Any]]:
    """검색 결과 파일에서 한 모드의 케이스를 색인한다."""
    data: dict[str, Any] = json.loads((RESULTS_DIR / name).read_text(encoding="utf-8"))
    return {str(case["case_id"]): case for case in data["modes"][mode]["cases"]}


def run(out_path: Path) -> None:
    """세 대조를 분석해 결과 파일에 쓴다."""
    results: dict[str, dict[str, dict[str, Any]]] = {
        "anchored": load_cases("impact-claude-sonnet-5-20260821T161119Z.json"),
        "deanchored": load_cases("impact-claude-sonnet-5-deanchored-20260821T194218Z.json"),
        "kure": load_retrieval("retrieval-kure-v1-20260820T231624Z.json", "VECTOR"),
        "bge": load_retrieval("retrieval-local-20260820T230056Z.json", "VECTOR"),
    }
    for index, name in enumerate(VARIABILITY_RUNS):
        results[f"repeat{index}"] = load_cases(name)

    comparisons = build(results)
    report: dict[str, Any] = {
        "method": "Miller 2024, arXiv:2411.00640 — eq (1)(4)(7)(8)(9)(10), Appendix C",
        "alpha": ALPHA,
        "power": POWER,
        "target_deltas": list(TARGET_DELTAS),
        "generated_for": "docs/23-metrics-summary.md",
        "comparisons": comparisons,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for entry in comparisons:
        logger.info(
            "%-42s n=%-3s 차이 %+.4f  SE %.4f (군집 %s)  CI95 [%+.4f, %+.4f]  McNemar p=%s",
            entry["name"],
            entry["n"],
            entry["mean_diff"],
            entry["se_paired"],
            entry["se_paired_clustered"],
            entry["ci95"][0],
            entry["ci95"][1],
            entry["mcnemar_exact_p"],
        )
        logger.info(
            "%-42s ω²=%.5f σ²=%.5f  필요 n(δ=0.05) %s / 군집보정 %s",
            "",
            entry["omega2"],
            entry["sigma2_a"],
            entry["required_n"]["0.05"],
            (entry["required_n_clustered"] or {}).get("0.05"),
        )
    logger.info("결과: %s", out_path)


def main() -> None:
    """진입점."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="검정력 분석 (docs/23 §3)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out: Path = args.out or RESULTS_DIR / f"power-analysis-{stamp}.json"
    run(out)


if __name__ == "__main__":
    main()
