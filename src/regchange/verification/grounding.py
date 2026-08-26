"""gate 3단의 판정 집계 — 판정들을 모아 **무엇을 다시 쓰고 무엇을 경고할지** 정한다.

목적:
    주장별 `SupportLevel` 판정 목록을 받아, (a) 재작성 대상 주장, (b) 검토 화면에 띄울
    경고, (c) 근거가 하나도 남지 않았는지를 순수 함수로 결정한다.

구현 이유:
    **임계치를 만들지 않는다.** 「`UNSUPPORTED` 비율이 X% 를 넘으면 강등」 같은 규칙을 넣고
    싶어지지만, 그 X 를 정할 근거가 지금 없다. 근거 없는 임계치는 근거 없는 상수이며
    (CLAUDE.md §4), 한 번 들어가면 나중에 측정해도 "원래 그 값이었으니까"로 남는다.
    지금 두는 규칙은 **관측이 아니라 계약에서 나오는 것 둘뿐**이다.

      | 규칙 | 어디서 나왔나 |
      |---|---|
      | `UNSUPPORTED` 주장은 재작성 대상이다 | 원칙 2 — 뒷받침하지 않는 근거는 근거가 아니다 |
      | `UNSUPPORTED` 가 하나라도 있으면 경고한다 | 4단계 지시 §3-2 |

    `PARTIAL` 은 **제거하지도 재작성하지도 않는다.** 부분 일치는 날조가 아니며, 담당자가
    보고 판단할 값이다. 다만 `SUPPORTED` 로 세지 않는다 — `SupportVerdict.supported` 가
    `PARTIAL` 에 거짓을 돌려주는 것과 같은 이유다.

    **비율을 계산해서 함께 돌려준다.** 임계치를 만들지 않는 것과 비율을 재지 않는 것은
    다르다. 재 두어야 골든셋 측정에서 분포를 보고 **다음에** 임계치를 정할 수 있다.
    재지 않으면 그 결정을 영원히 못 한다.

    **이 모듈은 I/O 도 도메인 객체도 모른다.** 입력이 `(주장 키, 등급, 이유)` 셋이므로
    `ImpactDraft` 를 import 하지 않는다 — 그 타입이 `adapters.llm` 을 끌고 오고, 그러면
    `verification` 의 I/O 격리 계약이 간접 경로로 깨진다. 초안을 실제로 잘라내는 일은
    도메인을 아는 `pipeline/impact.py` 가 한다.

트레이드오프:
    - 주장 키를 문자열로 받는다. 타입이 붙은 식별자보다 약하지만, 타입을 붙이려면 이
      모듈이 도메인 객체를 알아야 한다. 격리를 지키는 쪽을 택했다.
    - 판정 하나가 하나의 (주장, 인용) 쌍에 대응한다고 가정한다. 한 주장에 인용이 여럿이면
      호출부가 키를 나눠서 넘겨야 한다. 여기서 묶어 주면 "어느 인용이 떨어졌는가"가
      사라진다.

엣지 케이스:
    - **판정이 0건**: `needs_rewrite=False`, `warn=False`. 판정할 주장이 없었던 것이며,
      그것은 근거가 없다는 뜻이지 검증에 실패한 것이 아니다. 최종 상태는 gate 2단이
      이미 `INSUFFICIENT_EVIDENCE` 로 정했다.
    - **전부 `UNSUPPORTED`**: 전부 재작성 대상이다. 재작성 후에도 같으면 남는 주장이
      0건이 되고 호출부가 `INSUFFICIENT_EVIDENCE` 로 이관한다 (ADR-013).
    - **같은 키가 두 번**: 두 판정 모두 남는다. 합치지 않는다 — 어느 인용이 떨어졌는지
      세어야 하기 때문이다. 재작성 대상 키는 중복이 제거된다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from regchange.verification.base import ClaimRelation, SupportLevel


@dataclass(frozen=True, slots=True)
class ClaimJudgment:
    """주장 하나(정확히는 주장·인용 한 쌍)에 대한 판정."""

    key: str
    """호출부가 정한 주장 식별자. 재작성 대상을 지목하는 데 쓴다."""
    level: SupportLevel
    reason: str
    relation: ClaimRelation | None = None
    """de-anchored 검증기의 원본 판정. anchored 에서는 None.

    `level` 이 결정을 정하고 이 값은 **측정이 본다.** 둘을 하나로 합치면 `BEYOND` 와
    `UNRELATED` 의 비율을 볼 수 없고, 그 비율이 재설계가 무엇을 잡았는지 말해 준다."""


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """gate 3단의 집계 결과.

    목적:
        판정 분포와 후속 조치(재작성·경고)를 한 값으로 담는다.

    구현 이유:
        분포(`counts`)를 결과에 담는다. 골든셋 측정이 `SUPPORTED/PARTIAL/UNSUPPORTED`
        비율을 요구하며, 호출부가 다시 세면 세는 방법이 두 곳에 생긴다.

    트레이드오프:
        `unsupported_ratio` 를 계산해 두지만 **아무도 그것으로 분기하지 않는다.**
        지금은 측정용 값이며, 분기가 필요해지면 그때 근거와 함께 임계치를 만든다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    judgments: tuple[ClaimJudgment, ...]

    @property
    def counts(self) -> Mapping[SupportLevel, int]:
        """등급별 판정 수. 0인 등급도 키로 존재한다 — 빠진 키는 0과 구별되지 않는다."""
        tally = dict.fromkeys(SupportLevel, 0)
        for judgment in self.judgments:
            tally[judgment.level] += 1
        return tally

    @property
    def relation_counts(self) -> Mapping[ClaimRelation, int]:
        """관계별 판정 수. de-anchored 검증기에서만 채워진다.

        anchored 실행에서는 전부 0 이며, **그 사실 자체가 어느 검증기로 돌았는지를
        말해 준다** — 0 과 부재를 구별하기 위해 키는 항상 존재한다.
        """
        tally = dict.fromkeys(ClaimRelation, 0)
        for judgment in self.judgments:
            if judgment.relation is not None:
                tally[judgment.relation] += 1
        return tally

    @property
    def unsupported_keys(self) -> tuple[str, ...]:
        """재작성 대상 주장 키. 순서를 지키고 중복을 제거한다."""
        return tuple(
            dict.fromkeys(j.key for j in self.judgments if j.level is SupportLevel.UNSUPPORTED)
        )

    @property
    def unsupported_notes(self) -> tuple[str, ...]:
        """재작성 프롬프트에 넘길 판정 문장. **검증기의 판정만 넘긴다** (원칙 3)."""
        return tuple(
            f"{j.key}: {j.reason}" for j in self.judgments if j.level is SupportLevel.UNSUPPORTED
        )

    @property
    def needs_rewrite(self) -> bool:
        """뒷받침되지 않은 주장이 있는가. 있으면 evaluator-optimizer 가 1회 재작성한다."""
        return bool(self.unsupported_keys)

    @property
    def warn(self) -> bool:
        """검토 화면 상단에 경고를 띄울 것인가 (4단계 지시 §3-2)."""
        return self.needs_rewrite

    @property
    def unsupported_ratio(self) -> float | None:
        """`UNSUPPORTED` 비율. **분기에 쓰지 않는다** — 임계치를 정할 근거를 모으는 값이다.

        판정이 0건이면 `None` 이다. 0.0 으로 두면 "판정한 것이 전부 뒷받침됐다"와
        "판정할 것이 없었다"가 같은 값이 된다.
        """
        if not self.judgments:
            return None
        return len([j for j in self.judgments if j.level is SupportLevel.UNSUPPORTED]) / len(
            self.judgments
        )


def decide(judgments: Iterable[ClaimJudgment]) -> GroundingResult:
    """판정 목록을 집계한다. 판정을 바꾸지 않고 세기만 한다.

    목적:
        gate 3단의 후속 조치를 한 곳에서 결정한다.

    구현 이유:
        함수를 따로 두는 이유는 호출부가 `GroundingResult` 를 직접 만들지 않게 하기
        위해서다. 직접 만들면 판정 일부를 빼고 만드는 경로가 생기고, 그 경로는 "검증을
        통과했다"처럼 보인다.

    트레이드오프:
        지금은 튜플로 감싸는 것 외에 하는 일이 없다. 그럼에도 함수를 두는 이유는 위와
        같으며, 규칙이 늘어날 자리가 여기여야 하기 때문이다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    return GroundingResult(judgments=tuple(judgments))
