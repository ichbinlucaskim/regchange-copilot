"""gate 3단 — 의미 뒷받침 검증의 경계와 판정 어휘 (원칙 2, 3).

목적:
    "이 인용이 이 주장을 실제로 뒷받침하는가"를 판정하는 경계를 선언하고, 그 판정이
    쓰는 값(`SupportLevel`)을 정의한다. 구현체는 이 패키지 밖에 있다.

구현 이유:
    **이 패키지는 I/O 를 갖지 않는다** (CLAUDE.md §8, import-linter 계약). 판정에 모델
    호출이 필요하지만, 호출은 구현체가 주입받은 클라이언트로 하고 이 경계는 **판정 대상과
    판정 결과만** 다룬다. 그래서 `adapters` 를 import 하지 않는다 — 프롬프트 모듈조차
    import 하지 않는다. `prompts/grounding.py` 가 `adapters.llm` 의 타입을 쓰므로, 여기서
    그것을 import 하면 계약이 **간접 경로로** 깨진다. 대신 방향을 뒤집었다: 판정 어휘를
    여기서 정의하고 프롬프트가 그것을 가져다 쓴다.

    **`SupportLevel` 을 3값으로 둔다.** 2값이면 "소재는 겹치는데 핵심이 어긋난" 경우가
    어느 쪽으로든 뭉개진다. 골든셋 case-013 이 정확히 그 형태이며, 그것을 `SUPPORTED` 로
    두면 gate 가 통과 의식이 되고 `UNSUPPORTED` 로만 두면 정당한 부분 일치까지 재작성을
    부른다.

    **인터페이스가 먼저 있어야 하는 이유**: 인터페이스가 없으면 초안 생성 노드가 자기
    안에서 검증을 하게 된다. 생성기가 자기 출력을 검증하면 검증은 형식화된다 (원칙 3).
    경계가 먼저 있으면 그 경로가 만들어지지 않는다 — import-linter 가 `verification` 을
    I/O 로부터 격리하는 것과 같은 방식의 사전 차단이다.

트레이드오프:
    검증기가 판정만 하고 고쳐 쓰지 않는다. 고쳐 쓰면 검증기가 생성기가 된다. 그 대가로
    실패한 초안을 살릴 방법이 **재작성 1회**뿐이며, 그 이후는 이관이다 (ADR-013).

    `SupportVerdict` 에 신뢰도나 점수를 두지 않는다. 점수를 두면 임계값을 정하게 되고,
    임계값은 근거 없이 정해지기 쉽다. 등급 3값은 근거 없이 정할 자리가 없다.

엣지 케이스:
    - 검증기 실패·타임아웃: **실패는 통과가 아니다.** 구현체는 예외를 던지고 호출부가
      폐기한다. 조용히 `SUPPORTED` 를 돌려주는 구현은 이 계약 위반이다.
    - 인용이 0건인 주장: 여기까지 오지 않는다. gate 2단이 먼저 제거한다.
    - 판정이 애매한 경우: 낮은 등급으로 떨어뜨린다. 이 도메인에서 근거 없는 제안은
      놓친 제안보다 비싸다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SupportLevel(StrEnum):
    """뒷받침 정도 3값. 순서가 있으며 아래로 갈수록 근거가 약하다.

    **이것이 gate 3단의 결정 어휘다.** de-anchored 검증기는 `ClaimRelation` 으로
    판정하고 그 값이 여기로 사상된다 — 하류(재작성 분기·제거·상태 판정)가 한 어휘만
    보게 하기 위해서다. 원본 판정은 `ClaimJudgment.relation` 에 그대로 남는다.
    """

    SUPPORTED = "SUPPORTED"
    """인용 문단이 주장의 핵심을 직접 담고 있다."""
    PARTIAL = "PARTIAL"
    """일부는 담고 있으나 주장의 핵심 중 일부가 인용문에 없다.

    **de-anchored 검증기는 이 값을 내지 않는다.** 관계를 묻는 질문에는 "부분적으로
    관계있다"가 없기 때문이다 — 초안의 주장은 문단이 말할 수 있는 범위 **안이거나
    밖이거나** 다른 이야기다."""
    UNSUPPORTED = "UNSUPPORTED"
    """뒷받침하지 않는다. **소재만 겹치는 경우가 여기다** (case-013 의 실패 형태)."""


class ClaimRelation(StrEnum):
    """de-anchored 검증기의 판정. 정도가 아니라 관계를 묻는다.

    목적:
        "이 주장이 이 인용에 뒷받침되는가"가 아니라 "이 문단이 말할 수 있는 범위와 초안의
        주장이 어떤 관계인가"를 답한다.

    구현 이유:
        **`SupportLevel` 은 연속적이라 주장이 약해지면 위로 움직인다.** 4단계 측정에서
        관측된 실패가 정확히 그 형태다 — 재작성이 주장에 단서를 붙여 약화시키자 같은
        인용문에 대해 `UNSUPPORTED` 가 `SUPPORTED` 로 뒤집혔다
        (`docs/12-impact-assessment-results.md` §5.2). **약해진 주장은 실제로 더 그럴듯하고,
        정도를 묻는 질문은 그것을 통과시킨다.**

        관계를 물으면 기준이 초안이 아니라 **검증기가 먼저 낸 자기 답**이 된다. 주장이
        약해지면 `BEYOND` 에서 `WITHIN` 으로 가는 것이 아니라, 그 약해진 주장이 **자기가
        말한 범위 안에 있는지**를 다시 본다.

    트레이드오프:
        `PARTIAL` 에 해당하는 중간값이 없다. 부분적으로 넘어선 주장은 `BEYOND` 이며,
        기존 어휘보다 보수적이다. 그것이 재현율을 깎는지는 대조 측정이 답한다 —
        **이 enum 을 만든 것만으로는 아무것도 증명되지 않는다.**

    엣지 케이스:
        - 검증기가 먼저 낸 답이 비어 있는 경우(문단이 개정과 무관): 초안의 주장은
          `UNRELATED` 가 된다.
        - 초안의 주장이 검증기의 답보다 **좁은** 경우: `WITHIN` 이다. 좁은 주장은
          넘어서지 않았다.
    """

    WITHIN = "WITHIN"
    """초안의 주장이 이 문단으로 말할 수 있는 범위 안에 있다."""
    BEYOND = "BEYOND"
    """초안이 더 나아갔다. 이 문단이 말하지 않는 것을 주장한다."""
    UNRELATED = "UNRELATED"
    """다른 이야기다. 소재가 겹치더라도 이 문단이 다루는 것이 아니다."""


RELATION_TO_LEVEL: dict[ClaimRelation, SupportLevel] = {
    ClaimRelation.WITHIN: SupportLevel.SUPPORTED,
    ClaimRelation.BEYOND: SupportLevel.UNSUPPORTED,
    ClaimRelation.UNRELATED: SupportLevel.UNSUPPORTED,
}
"""관계 → 결정 어휘 사상.

**`BEYOND` 와 `UNRELATED` 를 같은 결정으로 보낸다.** 둘 다 "이 인용이 이 주장을 받치지
않는다"이며 조치가 같다. 구별이 사라지는 것은 아니다 — 원본 값이 `ClaimJudgment.relation`
에 남고 측정이 그것을 따로 센다."""


@dataclass(frozen=True, slots=True)
class SupportVerdict:
    """인용 하나가 주장을 뒷받침하는지에 대한 판정.

    `reason` 은 사람이 읽는 값이다. 판정을 뒤집는 근거로 쓰지 않는다 — 뒤집으려면
    다시 판정해야 한다.
    """

    level: SupportLevel
    reason: str

    relation: ClaimRelation | None = None
    """de-anchored 검증기가 낸 원본 판정. anchored 검증기에서는 None 이다.

    사상된 `level` 과 함께 보관하는 이유는 **측정이 `BEYOND` 와 `UNRELATED` 를 구별해서
    세야 하기 때문**이다. 사상만 남기면 두 값이 같은 것이 되고, 그러면 "초안이 넘어섰다"와
    "다른 이야기다"의 비율을 볼 수 없다."""

    @property
    def supported(self) -> bool:
        """`SUPPORTED` 인가. `PARTIAL` 은 **참이 아니다** — 부분 일치는 근거가 아니다."""
        return self.level is SupportLevel.SUPPORTED


class SupportVerifier(Protocol):
    """주장과 인용문을 받아 뒷받침 여부만 판정하는 경계.

    목적:
        gate 3단의 유일한 창구.

    구현 이유:
        **차단하는 것은 정보가 아니라 판단이다.** 검증기가 받아서는 안 되는 것은 생성기의
        프롬프트·추론·초안이지, 외부에서 온 입력이 아니다. 그래서 이 메서드는 주장·인용문·
        문단 표기만 받되, 구현체가 **개정 조문 원문**을 생성자에서 받는 것은 허용된다
        (`pipeline/grounding.py` 의 de-anchored 구현체). 개정 조문은 법제처가 준 사실이며
        우리 모델의 출력이 아니다.

        의무사항 추출 결과는 **넣지 않는다.** 개정 조문은 원문이지만 의무사항은 우리
        모델이 해석한 것이고, 그것이 넘어가면 검증기가 생성기의 판단을 물려받는다.
        경계는 "외부 입력인가, 우리 출력인가"다.

        `spec`(`ISP-GUIDE-003 제28조 (…)`)만 함께 받는다. 문단 ID 는 받지 않는다 —
        검증기가 ID 를 근거로 삼을 여지를 없앤다.

    트레이드오프:
        anchored 구현체는 문맥을 잃는다. 인용문만으로는 판정이 어려운 경우가 있고, 그때
        검증기는 보수적으로 떨어뜨린다. de-anchored 구현체는 개정 조문을 받아 그 문맥을
        되찾는 대신 **검증기가 보는 것이 늘어난다** — 늘어난 것이 외부 입력뿐이므로
        자기 선호 편향은 커지지 않지만, "입력을 최소로 둔다"는 원래 판단과는 반대다.
        방향 없는 요약은 대조에 쓸 수 없다는 것이 그 대가로 얻는 것이다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    async def verify(self, *, claim: str, quote: str, spec: str) -> SupportVerdict:
        """인용문이 주장을 뒷받침하는지 판정한다. 문장을 고쳐 쓰지 않는다."""
        ...
