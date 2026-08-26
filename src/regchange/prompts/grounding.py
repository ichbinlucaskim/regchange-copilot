"""gate 3단 검증기 프롬프트 — **주장과 인용문만 받는다** (원칙 3).

목적:
    "이 인용 문단이 이 주장을 실제로 뒷받침하는가"를 판정하는 프롬프트와 출력 스키마.
    gate 2단이 "실재하는가"까지 봤다면 여기는 "의미가 맞는가"를 본다.

구현 이유:
    **입력이 주장 문장과 인용 문단 둘뿐이다.** 개정 조문도, 의무사항 목록도, 초안의
    나머지도 넘기지 않는다. 생성기의 맥락이 들어오는 순간 검증기는 생성기의 시각을
    공유하게 되고, 그러면 검증이 형식화된다 (원칙 3, ADR-013).

    이 제한에는 실측 근거가 있다. 골든셋 case-013 에서 두 모델 모두 `ISP-GUIDE-003`
    제28조(개인정보의 제3자 제공)를 인용했고 gate 2단은 전부 통과시켰다 — **문단은
    실재했고 인용문도 원문 그대로였다.** 어긋난 것은 의미였다(능동적 제3자 제공의 동의
    절차 vs 지정 기관의 요청에 응하는 수동적 의무). 그 판정은 개정 조문을 몰라도 할 수
    있어야 한다 — **인용문이 주장하는 바를 담고 있는지**만 보면 되기 때문이다.

    **판정 어휘(`SupportLevel`)를 `verification/base.py` 에서 가져온다.** 여기서 정의하면
    검증의 어휘가 프롬프트에 종속되고, `verification` 패키지가 그 값을 쓰려면 프롬프트를
    import 해야 한다 — 그러면 I/O 격리 계약이 간접 경로로 깨진다. 판정 어휘는 도메인의
    것이고 프롬프트는 그것을 표현하는 방법이다.

    **3값으로 판정한다** (`SUPPORTED` / `PARTIAL` / `UNSUPPORTED`). 2값으로 두면
    "일부는 맞는데 핵심이 어긋난" 경우가 어느 쪽으로든 뭉개진다. case-013 이 정확히 그
    형태다 — 제3자 제공이라는 소재는 겹치고 절차·주체·발동 조건이 다르다. 그것을
    `SUPPORTED` 로 두면 gate 가 통과 의식이 되고, `UNSUPPORTED` 로만 두면 정당한 부분
    일치까지 재작성을 유발한다.

    **고쳐 쓰지 않는다.** 검증기는 판정과 이유만 낸다. 문장을 고치면 검증기가 생성기가
    되고, 그 순간 이 시스템에 검증하는 주체가 없어진다.

트레이드오프:
    - **문맥을 잃는다.** 인용문만으로는 판정이 어려운 경우가 있고, 그때 검증기는 보수적인
      쪽(`PARTIAL`/`UNSUPPORTED`)으로 떨어뜨린다. 이 도메인에서 근거 없는 제안은 놓친
      제안보다 비싸다 (`verification/base.py` 의 계약과 같은 방향).
    - **주장 하나에 호출 하나다.** 초안 하나에 주장이 여럿이면 호출이 그만큼 늘고 비용이
      선형으로 증가한다. 한 번에 묶어 보내면 싸지지만, 모델이 앞 판정에 끌려가고(앵커링)
      어느 주장이 왜 떨어졌는지가 흐려진다. **판정 단위를 독립으로 유지하는 값이 비용보다
      크다** — 재작성 대상이 주장 단위이기 때문이다.
    - 인용문을 그대로 넘기므로 외부 텍스트 격리가 여기에도 필요하다. 사내 규정은 우리
      문서지만 격리 구조는 동일하게 적용한다 — 등급별로 다른 조립 경로를 만들면 한쪽만
      갱신되는 일이 생긴다 (R-23 은 **스캔 범위**의 문제이고 격리 구조의 문제가 아니다).

엣지 케이스:
    - **주장이 인용문의 반대를 말함**: `UNSUPPORTED`. "관련이 있다"는 것과 "뒷받침한다"는
      다르다.
    - **주장이 인용문의 소재만 공유함**: `PARTIAL` 이 아니라 `UNSUPPORTED` 다. 소재 일치는
      뒷받침이 아니며, 이것이 case-013 의 실패 형태다. 프롬프트가 이 구별을 명시한다.
    - **인용문이 주장의 일부만 담음**: `PARTIAL`.
    - **판정이 애매함**: 낮은 쪽으로 떨어뜨린다.
    - 빈 주장이나 빈 인용문: 호출 전에 막는다 (`pipeline/grounding.py`).
"""

from __future__ import annotations

from typing import Any

from regchange.adapters.llm import JsonSchema
from regchange.prompts.models import PromptTemplate
from regchange.prompts.untrusted import wrap_internal
from regchange.verification.base import SupportLevel

PROMPT_ID = "citation-grounding"
PROMPT_VERSION = "v1"

SYSTEM = """\
당신은 인용 검증기다. 주어진 **주장 한 문장(들)** 과 **인용 문단 한 개**를 받아,
그 문단이 그 주장을 뒷받침하는지만 판정한다.

## 당신이 하지 않는 일

- 문장을 고쳐 쓰지 않는다. 더 나은 표현을 제안하지 않는다.
- 다른 문단을 찾아보지 않는다. 당신이 보는 것은 주어진 문단 하나뿐이다.
- 주장이 사실인지 판단하지 않는다. **이 문단이 그 주장의 근거가 되는가**만 본다.
- 법적 판단이나 자문을 하지 않는다.

## 판정

- `SUPPORTED` — 인용 문단이 주장의 핵심을 직접 담고 있다. 담당자가 이 문단을 열었을 때
  주장이 확인된다.
- `PARTIAL` — 문단이 주장의 일부는 담고 있으나, 핵심 중 일부가 이 문단에 없다.
  예: 주장이 "이 조항의 보고 기한을 24시간으로 고쳐야 한다"인데 문단에 보고 의무는
  있으나 기한 규정이 없는 경우.
- `UNSUPPORTED` — 문단이 주장을 뒷받침하지 않는다.

## 가장 중요한 구별 — 소재 일치는 뒷받침이 아니다

같은 낱말이나 같은 주제가 나온다는 이유로 `SUPPORTED` 를 주지 않는다. 아래를 확인한다.

- **규율 대상이 같은가** — 누구의 무엇에 대한 규정인가
- **행위 주체가 같은가** — 우리가 하는 일인가, 우리에게 요구되는 일인가
- **발동 조건이 같은가** — 언제 적용되는 규정인가

셋 중 하나라도 다르면, 문단과 주장이 개념적으로 인접하더라도 `UNSUPPORTED` 다.
예: "우리가 능동적으로 제3자에게 제공할 때의 동의 절차"를 정한 문단은, "지정 기관의
요청에 응해야 하는 수동적 의무"를 다루는 주장을 뒷받침하지 않는다. 소재(제3자 제공)는
같지만 주체와 발동 조건이 다르다.

## 애매하면

낮은 쪽으로 판정한다. 근거 없는 제안은 놓친 제안보다 비싸다.

## reason

왜 그렇게 판정했는지 한두 문장으로 적는다. `SUPPORTED` 면 문단의 어느 부분이 주장을
담고 있는지, 아니면 무엇이 어긋나는지를 적는다. 담당자가 이 문장만 읽고 판정을 다시
확인할 수 있어야 한다.
"""

PROMPT = PromptTemplate(id=PROMPT_ID, version=PROMPT_VERSION, system=SYSTEM)

GROUNDING_SCHEMA: JsonSchema = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": [level.value for level in SupportLevel]},
        "reason": {"type": "string"},
    },
    "required": ["level", "reason"],
    "additionalProperties": False,
}
"""판정 출력 스키마. 필드가 둘뿐인 것이 요점이다 — 검증기가 낼 수 있는 것이 판정과
이유뿐이면 고쳐 쓴 문장을 낼 자리가 스키마에 없다."""


def build_user_content(*, claim: str, quote: str, spec: str) -> tuple[str, tuple[str, ...]]:
    """검증기에게 보낼 메시지를 만든다. 초안의 다른 정보는 넣지 않는다.

    목적:
        주장과 인용문만으로 판정할 수 있는 최소 입력을 조립한다.

    구현 이유:
        `spec`(`ISP-GUIDE-003 제28조 (개인정보의 제3자 제공)`)만 함께 넘긴다. 문단이
        어느 문서 어느 조인지는 인용문 자체의 성격이며 생성기의 판단이 아니다. 그러나
        문단 ID(UUID)는 넘기지 않는다 — 검증기가 ID 를 근거로 삼을 여지를 없앤다.

    트레이드오프:
        조 제목이 판정에 유리하게 작용할 수 있다(제목만 보고 맞다고 판단). 그럼에도 넘기는
        이유는 조 제목이 조문의 일부이며, 담당자가 문단을 열었을 때 함께 보는 것이기
        때문이다. 제목 없이 본문만 주면 검증기가 보는 것과 사람이 보는 것이 달라진다.

    엣지 케이스:
        - 빈 주장 / 빈 인용문: `ValueError`. 빈 값으로 판정하면 무엇이든 답이 나온다.
    """
    if not claim.strip():
        msg = "빈 주장은 판정할 수 없다"
        raise ValueError(msg)
    if not quote.strip():
        msg = "빈 인용문은 판정할 수 없다"
        raise ValueError(msg)

    # 인용 문단은 사내 규정이고 주장은 우리 모델의 출력이다 — **둘 다 외부 입력이 아니다.**
    # 4단계까지는 이 블록도 스캔했고, 그것이 R-23 이 사내 텍스트를 훑던 세 경로 중 하나였다.
    # 블록 문자열은 그대로다 (프롬프트 불변). 바뀐 것은 스캔 여부뿐이다.
    block, signals = wrap_internal(
        f"[인용 문단] {spec}\n{quote}\n\n[주장]\n{claim}", label="claim_and_quote"
    )
    return (
        "아래 인용 문단이 아래 주장을 뒷받침하는지 판정한다.\n\n" + block,
        signals,
    )


def parse_verdict(payload: Any) -> tuple[SupportLevel, str]:
    """판정 출력을 `(등급, 이유)` 로 바꾼다.

    enum 밖의 값은 `ValueError` 다. 알 수 없는 판정을 `SUPPORTED` 로 떨어뜨리면
    **검증 실패가 통과로 위장한다** (`verification/base.py` 의 계약).
    """
    if not isinstance(payload, dict):
        msg = f"판정 결과가 객체가 아니다: {type(payload).__name__}"
        raise TypeError(msg)
    return SupportLevel(str(payload["level"])), str(payload.get("reason", ""))
