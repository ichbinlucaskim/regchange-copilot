"""프롬프트 템플릿 모델 — 원본을 해시로 고정한다.

목적:
    프롬프트를 식별자·버전·본문·해시를 가진 값으로 표현한다. `llm_invocation` 이
    `prompt_template_id` 와 `prompt_template_sha256` 을 요구하므로, 그 값을 만드는 곳이
    프롬프트 정의 자체여야 한다.

구현 이유:
    **해시를 저장하지 않고 매번 계산한다.** 손으로 적어 둔 해시는 본문을 고친 뒤 갱신을
    잊으면 조용히 거짓말을 한다. 계산하면 본문이 바뀌는 순간 해시가 바뀌고, 그 사실이
    기록에 그대로 나타난다.

    **버전을 손으로 올린다.** 해시가 자동이면 버전도 자동으로 두고 싶어지지만, 버전은
    "의도적으로 바꿨다"는 선언이다. 오타 수정과 판단 기준 변경이 같은 값으로 표현되면
    "언제부터 다른 기준으로 판정했는가"에 답할 수 없다. 해시는 사실, 버전은 의도다.

트레이드오프:
    버전을 올리는 것을 잊을 수 있다. 그 경우 해시만 바뀌므로 **기록상으로는 구별
    가능하다** — 같은 버전에 두 해시가 있으면 버전 관리가 실패한 것이고, 그 사실이
    질의로 드러난다. 검증을 강제하는 대신 드러나게 두었다.

엣지 케이스:
    - 본문이 빈 문자열: `ValueError`. 빈 프롬프트로 호출하면 모델이 무엇이든 답한다.
    - `system` 에 외부 텍스트를 끼워 넣는 경로를 만들지 않는다. 템플릿의 `system` 은
      상수이며 포매팅 자리가 없다 (기획서 10.1 untrusted 격리).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """프롬프트 하나. 시스템 지침은 상수이고 외부 텍스트가 섞이지 않는다.

    목적:
        모델 호출에 필요한 지침과, 그 호출을 사후에 식별할 값을 함께 담는다.

    구현 이유:
        `system` 만 갖고 사용자 메시지 템플릿을 갖지 않는다. 사용자 메시지는 외부
        텍스트를 감싸는 구조이며 `prompts/untrusted.py` 가 만든다 — 지침과 데이터를
        같은 객체가 조립하면 경계가 흐려진다.

    트레이드오프:
        호출부가 두 곳(템플릿, 격리 함수)을 봐야 한다. 그 분리가 격리의 실체다.

    엣지 케이스:
        - 빈 `system`: `ValueError`.
    """

    id: str
    version: str
    system: str

    def __post_init__(self) -> None:
        """빈 지침을 허용하지 않는다."""
        if not self.system.strip():
            msg = f"{self.id}: 시스템 지침이 비어 있다"
            raise ValueError(msg)

    @property
    def sha256(self) -> str:
        """본문 해시. 매번 계산하므로 본문과 어긋날 수 없다."""
        return hashlib.sha256(self.system.encode("utf-8")).hexdigest()
