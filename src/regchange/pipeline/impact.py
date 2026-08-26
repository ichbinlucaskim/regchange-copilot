"""영향평가 초안 → gate → 재작성 1회 → 최종 판정 (ADR-013 의 evaluator-optimizer).

목적:
    3단계가 만든 의무사항과 검색 결과를 받아 영향평가 초안을 만들고, 네 겹의 gate 를
    통과시킨 뒤 **코드가 최종 상태를 정한다.** 초안·검증·재작성의 모든 호출이
    `llm_invocation` 에 남는다.

구현 이유:
    **단계를 함수로 쪼개 두고 루프를 밖에 둔다.** `draft_once` / `verify_draft` /
    `finalize` 가 각각 독립 함수이고, `assess_impact` 는 그것들을 순서대로 부르는
    얇은 오케스트레이터다. LangGraph 노드도 같은 함수를 부른다 (`graph/nodes.py`).

    이렇게 나눈 이유는 **그래프가 진짜 노드 경계를 갖게 하기 위해서**다. 초안·검증·분기를
    한 함수 안에 두고 노드 하나로 감싸면, 그래프 그림에는 세 노드가 있는데 실제로는 하나가
    돌게 된다. 그러면 체크포인트가 중간 상태를 갖지 못하고, "검증 직후 재개"가 불가능해지며,
    그림과 실행이 다르다는 사실이 아무 오류도 내지 않는다.

    **gate 순서가 싼 것부터다.**

      | 순서 | gate | 비용 | 무엇을 잡는가 |
      |---|---|---|---|
      | 1 | 스키마 강제 | 0 (API) | 모양이 틀린 출력 |
      | 2 | 정합성 검사 | 0 (코드) | 자기 입력에 없는 것을 가리키는 출력 |
      | 3 | 인용 실재 대조 | 0 (코드) | 지어낸 문단 ID, 변형된 인용문 |
      | 4 | 의미 뒷받침 검증 | 호출 1회/주장 | 실재하는 문단을 **잘못 고른** 출력 |

    4번만 모델을 쓴다. **1~3 이 잡을 수 있는 것을 4번에 맡기지 않는다** — 그러면 gate 가
    아니라 또 하나의 생성 단계가 된다 (ADR-013). 다만 기준선 §11 이 「싸고 확실한 1번」으로
    예고한 모순 필터는 전수 관측에서 기각됐고(`guards/consistency.py`), 그래서 2번이
    실제로 줄이는 호출은 거의 없다. **예고된 절감은 일어나지 않았다.**

    **재작성을 1회로 제한한다** (ADR-013). 검증 실패가 반복된다는 것은 근거가 부족하다는
    뜻이고, 그러면 고쳐 쓰는 것이 아니라 사람에게 넘기는 것이 맞다. 근거가 없는데 다시
    쓰면 **더 그럴듯한 문장**이 나올 뿐이며 그것이 F-6 을 악화시킨다.

    **ADR-013 의 「여전히 실패면 `INSUFFICIENT_EVIDENCE` 로 이관」을 주장 단위로 읽는다.**
    재작성 후에도 `UNSUPPORTED` 인 주장은 제거하고, **남은 영향 문단이 0건일 때** 이관한다.
    초안 전체를 버리지 않는 이유는 gate 2단이 인용 단위로 폐기하는 것과 같다 — 주장 하나가
    떨어졌다고 근거가 실재하는 나머지를 버리면 그 폐기가 "영향 없음"으로 보인다.
    주장이 하나라도 떨어졌으면 상태는 `NEEDS_REVIEW` 이고 검토 화면이 그것을 표시한다.

    **재작성 횟수를 `llm_invocation.revision` 에 남긴다.** "몇 %가 재작성을 거쳤는가"가
    ADR-013 의 「틀렸음을 알게 되는 신호 2번」이며, 비율이 높으면 루프를 늘릴 것이 아니라
    프롬프트나 검색을 의심해야 한다.

    **검증기가 초안을 받지 않는다.** `RecordingSupportVerifier` 는 생성자에서도 메서드에서도
    초안을 받지 않으며, 넘길 수단 자체가 없다 (원칙 3).

트레이드오프:
    - 부서 배정도 의미 검증을 받지만 **재작성을 유발하지 않는다.** 재작성 단위가 영향
      주장이기 때문이며, 부서 하나 때문에 초안 전체를 다시 쓰는 것은 비용이 이익보다 크다.
      뒷받침되지 않은 부서는 제거되고 기록에 남는다 — 근거 없는 배정을 통과시키는 것보다
      제거하는 쪽이 원칙 2에 맞다.
    - 검증 호출이 주장 수에 비례한다. 최악의 경우 재작성으로 두 배가 된다. ADR-013 이
      "최악의 경우 호출이 2배"라고 적은 그 지점이며, 실제 배수는 재작성 발생률이 정한다.
    - 단계를 쪼개면서 컨텍스트 객체(`DraftContext`)가 생겼다. 인자 목록을 매번 반복하는
      대신 한 번 만들어 넘기며, **불변으로 두어** 노드 사이에서 값이 바뀌지 않게 했다.

엣지 케이스:
    - **의무사항 0건**: 초안을 만들되 그 사실을 프롬프트에 명시한다. 의무가 없으면 영향
      문단도 없어야 하며, 결과는 `INSUFFICIENT_EVIDENCE` 가 된다.
    - **검색 후보 0건**: 인용할 대상이 없으므로 gate 2단이 전부 폐기하고 이관한다.
    - **스키마 위반 2회**: `INSUFFICIENT_EVIDENCE` 로 끝낸다. 세 번째를 시도하지 않는다.
    - **검증 호출 실패**: 그 평가를 이관하되 `verification_error` 에 사유를 남긴다.
      "근거가 없어서 이관"과 "검증하지 못해서 이관"은 조치가 다르다.
    - **재작성했는데 더 나빠짐**: 재작성 결과를 쓴다. 되돌리려면 두 초안을 비교해 고르는
      판정자가 필요하고 그 판정자가 없다. 두 초안 모두 `llm_invocation` 에 남는다.
    - **영향 문단은 살았는데 부서가 전부 폐기됨**: 정상 결과다. 부서를 채우기 위해 문서
      소관 부서를 넣지 않는다 — 그러면 배정이 항상 같은 값이 되어 정보가 아니게 된다.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import psycopg

from regchange.adapters.llm import LLMClient, LLMError, SchemaViolationError
from regchange.adapters.storage import DocumentStore
from regchange.audit.invocation import (
    InvocationOutcome,
    InvocationPurpose,
    InvocationRecord,
    record_invocation,
)
from regchange.guards.citations import (
    DraftGateResult,
    GateResult,
    enforce_draft_citations,
)
from regchange.guards.consistency import ConsistencyReport, check_draft
from regchange.guards.killswitch import Switch, SwitchGate
from regchange.pipeline.grounding import DeAnchoredSupportVerifier, RecordingSupportVerifier
from regchange.prompts.impact import (
    IMPACT_SCHEMA,
    PROMPT,
    Confidence,
    DraftStatus,
    ImpactDraft,
    RiskLevel,
    build_user_content,
    parse_draft,
)
from regchange.prompts.obligation import ObligationType
from regchange.retrieval.models import RetrievalResult, RetrievalSource
from regchange.verification.grounding import ClaimJudgment, GroundingResult, decide

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
"""스키마 위반 시 총 시도 횟수. 3단계와 같은 값이며 같은 이유다 — 두 번 실패한 프롬프트가
세 번째에 성공하는 것은 개선이 아니라 우연이다."""


class GroundingMode(StrEnum):
    """gate 3단을 어느 검증기로 돌 것인가.

    목적:
        같은 파이프라인 자리에 두 검증기를 갈아 끼워 **같은 골든셋으로 대조**할 수 있게 한다.

    구현 이유:
        기본값을 `ANCHORED` 로 둔다. 4단계 측정이 그 구현으로 나왔고, 기본값을 먼저 바꾸면
        "무엇이 달라졌는지"의 기준선이 사라진다. **측정이 기본값을 바꾼다, 그 반대가 아니다.**

    트레이드오프:
        두 구현을 유지하는 비용이 든다. ADR-016 이 `VECTOR`/`LEXICAL` 경로를 남긴 것과 같은
        판단이며, 지우면 다음 반영에서 같은 비교를 할 수 없다.

    엣지 케이스:
        - `DE_ANCHORED` 인데 개정 조문이 없음: 검증기 생성에서 `ValueError`. 방향 없는
          블라인드는 일반 요약이 되어 판정 기준이 되지 못한다.
    """

    ANCHORED = "ANCHORED"
    """주장과 인용문을 함께 보고 뒷받침 **정도**를 판정한다 (4단계 원래 구현)."""
    DE_ANCHORED = "DE_ANCHORED"
    """자기 답을 먼저 내고 초안의 주장과의 **관계**를 판정한다 (4단계 보강)."""


MAX_REVISIONS = 0
"""evaluator-optimizer 재작성 상한. **1 → 0 으로 내렸다 (2026-08-21). 측정이 근거다.**

ADR-013 은 이 값을 1로 정하며 "근거가 없는데 다시 쓰면 더 그럴듯한 문장이 나올 뿐"을
우려했다. 4단계 보강 측정이 그 우려를 확인했고, **예상보다 나빴다.**

**재작성이 새 근거를 찾은 사례가 15건 전수에서 0건이다** — anchored 5건 + de-anchored
10건의 원본 출력을 전부 대조했고, 새로 인용한 문단이 하나도 없었다
(`docs/12-impact-assessment-results.md` §12). 재작성이 실제로 하는 일은 **주장을 인용문에
맞춰 낮추는 것**이고, 그러면 같은 인용에 대한 판정이 뒤집힌다.

**그 뒤집힘은 정답과 오답을 가리지 않는다.**

| 케이스 | 재작성이 한 일 | 결과 |
|---|---|---|
| 002 | 주장에 "조문 문언상" 삽입 | `UNSUPPORTED` → `PARTIAL`, **정답 문단이 살아남음** |
| 013 | 부서 근거에 "…확인되지 않는다" 삽입 | `UNSUPPORTED` → `SUPPORTED`, **decoy 가 살아남음** |

같은 기전이 한 번은 맞고 한 번은 틀렸다. **기전이 정확성과 무관하므로 기댈 수 없다.**

**0 으로 두는 대가**: IMPACT 적중이 9/10 → 8/10 으로 준다(case-002를 잃는다). 이관은
4건 → 5건이 된다. **그 대가를 치른다** — 이 저장소의 손실함수에서 "모른다"(F-1)가
"그럴듯하게 틀림"(F-6)보다 싸고, 잃는 케이스는 **약화된 주장이 통과해서** 지켜지던 것이다.

**코드 경로는 남긴다.** 0 이 아닌 값을 주면 루프가 그대로 돈다 — ADR-016 이 `VECTOR`/
`LEXICAL` 경로를 남긴 것과 같은 이유이며, 지우면 다음 반영에서 같은 비교를 할 수 없다.
값을 올리려면 **재작성이 새 근거를 찾는다는 관측**이 먼저 있어야 한다."""


class AssessmentStatus(StrEnum):
    """영향평가의 최종 상태. 모델이 아니라 코드가 정한다."""

    OK = "OK"
    """근거가 실재하고 의미도 뒷받침된 영향 문단이 남았다."""
    NEEDS_REVIEW = "NEEDS_REVIEW"
    """초안은 남았으나 뒷받침되지 않아 제거된 주장이 있다. 검토 화면 상단에 경고한다."""
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    """남은 영향 문단이 0건이다. "영향이 없다"가 아니라 **"모른다"** 이다."""


@dataclass(frozen=True, slots=True)
class DraftContext:
    """한 평가 동안 바뀌지 않는 입력 전부를 담는 컨텍스트.

    목적:
        초안·검증 단계가 공유하는 값을 한 번 만들어 불변으로 들고 다닌다.

    구현 이유:
        노드가 여럿이면 같은 인자를 여러 번 넘기게 되고, 한 노드에서 값이 바뀌면 다음
        노드가 다른 입력으로 돈다. 불변으로 두면 그 일이 일어날 수 없다 —
        `RetrievedChunk` 를 불변으로 둔 것과 같은 이유다.

    트레이드오프:
        `retrieval` 전체를 들고 있어 객체가 크다. 필요한 것만 뽑아 담으면 작아지지만,
        승격 표시나 `searched_scope` 가 나중에 필요해질 때 다시 원본을 찾아야 한다.

    엣지 케이스:
        - 검색 결과가 0건: `candidates` 와 `retrieved` 가 비어 있고, 그 상태로도 초안
          호출은 이루어진다. "후보 0건"임을 프롬프트가 명시한다.
    """

    assessment_id: UUID
    law_name: str
    article_path: str
    revision_kind: str
    change_type: str
    after_text: str
    obligation_rows: tuple[tuple[str, str, str], ...]
    retrieval: RetrievalResult
    document_versions: dict[str, str] = field(default_factory=dict)

    @property
    def candidates(self) -> list[tuple[str, str, str, str | None]]:
        """프롬프트에 넣을 후보 문단. 승격 문단에는 표시가 붙는다."""
        return [
            (
                str(chunk.paragraph_id),
                f"{chunk.doc_id} {chunk.spec}",
                chunk.text_raw,
                _promotion_note(chunk),
            )
            for chunk in self.retrieval.chunks
        ]

    @property
    def retrieved(self) -> dict[str, str]:
        """Gate 2단의 대조 대상 — `{문단 ID: 원문}`."""
        return {str(c.paragraph_id): c.text_raw for c in self.retrieval.chunks}

    @property
    def specs(self) -> dict[str, str]:
        """검증기에 넘길 문단 표기. **문단 ID 는 넘기지 않는다** (원칙 3)."""
        return {str(c.paragraph_id): f"{c.doc_id} {c.spec}" for c in self.retrieval.chunks}

    @property
    def chunk_ids(self) -> list[UUID]:
        """`llm_invocation.retrieved_chunk_ids` — 이 호출이 인용을 허용받은 집합."""
        return [c.paragraph_id for c in self.retrieval.chunks]


def build_context(
    *,
    law_name: str,
    article_path: str,
    revision_kind: str,
    change_type: str,
    after_text: str,
    obligations: GateResult,
    retrieval: RetrievalResult,
    document_versions: dict[str, str] | None = None,
    assessment_id: UUID | None = None,
) -> DraftContext:
    """3단계 결과에서 평가 컨텍스트를 만든다.

    목적:
        의무사항 목록의 순서를 여기서 고정한다. `obligation_index` 가 그 순서를 가리킨다.

    구현 이유:
        `supported` 다음에 `unsupported` 를 둔다. **근거가 없는 의무를 버리지 않는 이유**는
        골든셋 case-013 이다 — "의무는 실재하는데 담을 사내 조항이 없다"가 정답인 경우가
        있고, 그 판정에는 그 의무가 목록에 있어야 한다.

    트레이드오프:
        순서가 두 집합의 연결로 정해지므로, 3단계 gate 의 분류가 바뀌면 인덱스 의미가
        바뀐다. 인덱스를 안정시키려면 의무에 고유 id 를 주어야 하는데, 그 id 는 모델이
        만들거나 우리가 부여해야 하고 둘 다 지금 근거가 없다.

    엣지 케이스:
        - 두 집합 모두 비어 있음: 빈 목록이며 프롬프트가 "의무 0건"을 명시한다.
    """
    return DraftContext(
        assessment_id=assessment_id or uuid4(),
        law_name=law_name,
        article_path=article_path,
        revision_kind=revision_kind,
        change_type=change_type,
        after_text=after_text,
        obligation_rows=tuple(
            (o.obligation_type.value, o.summary, o.source_span)
            for o in (*obligations.supported, *obligations.unsupported)
        ),
        retrieval=retrieval,
        document_versions=document_versions or {},
    )


@dataclass(frozen=True, slots=True)
class VerificationRun:
    """gate 3단 한 회차의 결과와 그 회차가 쓴 자원.

    호출 수를 함께 담는 이유는 **de-anchored 가 호출을 늘리기 때문**이다. 늘어난 양을
    세지 않으면 "무엇을 잡았는가"만 남고 "얼마를 썼는가"가 남지 않는다.
    """

    grounding: GroundingResult
    invocation_ids: tuple[UUID, ...]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    """캐시에서 읽은 입력 토큰. `input_tokens` 와 겹치지 않는다 (프롬프트 캐싱, 2026-08-24)."""
    cache_creation_tokens: int = 0
    """캐시에 쓴 입력 토큰. 단가가 읽기와 달라 합쳐 세지 않는다."""
    blind_calls: int = 0
    """de-anchored 1단계 호출 수. anchored 에서는 0이다."""
    blind_cache_hits: int = 0
    """같은 문단을 다시 인용해 1단계를 건너뛴 횟수."""


@dataclass(frozen=True, slots=True)
class DraftStep:
    """초안 한 회차의 결과 — 생성 + 정합성 + 인용 실재 대조까지."""

    draft: ImpactDraft
    raw_output: str | None
    """모델이 낸 원본 JSON. 재작성 프롬프트가 이것을 그대로 되돌려 준다."""
    consistency: ConsistencyReport
    gate: DraftGateResult
    attempts: int
    invocation_ids: tuple[UUID, ...]
    injection_signals: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    """캐시에서 읽은 입력 토큰. `input_tokens` 와 겹치지 않는다 (프롬프트 캐싱, 2026-08-24)."""
    cache_creation_tokens: int
    """캐시에 쓴 입력 토큰. 단가가 읽기와 달라 합쳐 세지 않는다."""
    failed: bool
    """모델 호출이나 파싱이 끝내 실패했는가. 이 경우 초안은 빈 값이다."""


@dataclass(frozen=True, slots=True)
class ImpactOutcome:
    """영향평가 한 건의 결과 전체. 폐기된 것과 실패도 결과다."""

    assessment_id: UUID
    status: AssessmentStatus
    draft: ImpactDraft
    """gate 를 통과한 항목만 남은 최종 초안. 원본은 `llm_invocation` 의 원본 출력에 있다."""
    consistency: ConsistencyReport
    gate: DraftGateResult
    grounding: GroundingResult
    revisions: int
    """실제로 수행한 재작성 횟수. 0 이면 첫 초안이 검증을 통과했다."""
    attempts: int
    """스키마 위반 재시도를 포함한 생성기 호출 수."""
    invocation_ids: tuple[UUID, ...]
    injection_signals: tuple[str, ...]
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    cache_read_tokens_total: int = 0
    """이 평가가 캐시에서 읽은 입력 토큰 합. **비용 집계가 이 값을 반드시 읽어야 한다** —
    캐시 도입 후 `input_tokens_total` 은 브레이크포인트 이후 토큰만 담는다."""
    cache_creation_tokens_total: int = 0
    """이 평가가 캐시에 쓴 입력 토큰 합. 읽기(0.1배)와 단가가 다르다(1.25배)."""
    blind_calls: int = 0
    """de-anchored 1단계 호출 수. anchored 로 돌면 0이다."""
    blind_cache_hits: int = 0
    """1단계를 건너뛴 횟수. 같은 문단을 여러 주장이 인용했다는 뜻이다."""
    verification_error: str | None = None
    """검증 호출 자체가 실패했으면 그 사유. **"근거가 없어서 이관"과 구별하는 값이다.**"""

    @property
    def removed_claims(self) -> tuple[str, ...]:
        """검증에서 떨어져 제거된 주장들. 검토 화면의 경고가 이것을 보여준다."""
        return self.grounding.unsupported_keys


async def draft_once(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    ctx: DraftContext,
    llm: LLMClient,
    store: DocumentStore,
    revision: int = 0,
    previous_raw: str | None = None,
    unsupported_notes: tuple[str, ...] = (),
) -> DraftStep:
    """초안을 한 번 만들고 코드 gate 두 개(정합성·인용 실재)를 적용한다.

    목적:
        모델 호출 없이 판정할 수 있는 것을 여기서 전부 판정한다. 이 함수를 통과한 초안만
        gate 3단(모델 호출)으로 간다.

    구현 이유:
        `revision > 0` 이면 이전 초안과 **검증기의 판정 문장만** 함께 넘긴다. 무엇이 왜
        떨어졌는지 알려주지 않고 다시 쓰라고 하면 같은 주장이 같은 근거로 다시 나온다.
        검증기의 프롬프트나 추론은 넘기지 않는다 — 생성기가 검증기를 흉내 내기 시작하면
        원칙 3 의 분리가 무너진다.

    트레이드오프:
        정합성 검사와 인용 대조를 한 함수에 묶었다. 나누면 노드가 둘 늘어나는데, 둘 다
        비용이 0 이고 사이에서 재개할 이유가 없다. **재개 지점은 비싼 경계에 둔다.**

    엣지 케이스:
        - 스키마 위반 2회: 빈 초안(`failed=True`)을 돌려준다. 예외를 던지지 않는 이유는
          그 상태도 결과이며 기록과 함께 하류로 흘러야 하기 때문이다.
        - 거부: 재시도하지 않는다. 같은 입력에 같은 판단이 나온다. 빈 초안으로 이관한다.
        - **호출 실패(ERROR)**: 재시도하지 않고 **예외를 전파한다.** 빈 초안으로 흘리면
          "모델을 못 불렀다"가 "근거가 부족하다"로 위장한다 (2026-08-22 사건).
    """
    await switches.require(Switch.LLM)
    user_content, wrap_signals = build_user_content(
        law_name=ctx.law_name,
        article_path=ctx.article_path,
        revision_kind=ctx.revision_kind,
        change_type=ctx.change_type,
        after_text=ctx.after_text,
        obligations=[tuple(row) for row in ctx.obligation_rows],  # type: ignore[misc]
        candidates=ctx.candidates,
        previous_draft=previous_raw if revision else None,
        unsupported_notes=unsupported_notes,
    )
    # 조립이 끝난 문자열을 다시 훑지 않는다 (R-23 ②). `pipeline/obligations.py` 와 같다.
    signals = tuple(sorted(set(wrap_signals)))
    if signals:
        logger.warning("입력에서 지시 유도 패턴 신호: %s", ", ".join(signals))

    invocation_ids: list[UUID] = []
    parsed: ImpactDraft | None = None
    raw_output: str | None = None
    attempt = 0
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0

    last_outcome: InvocationOutcome | None = None
    last_error: str | None = None
    while attempt < MAX_ATTEMPTS and parsed is None:
        attempt += 1
        outcome, parsed, raw_output, record = await _attempt(
            llm=llm,
            user_content=user_content,
            attempt=attempt,
            revision=revision,
            ctx=ctx,
            signals=signals,
        )
        async with conn.transaction():
            invocation_ids.append(await record_invocation(conn, store, record))
        # 기록은 즉시 커밋한다 (`pipeline/obligations.py` 와 같은 이유).
        await conn.commit()
        input_tokens += record.input_tokens or 0
        output_tokens += record.output_tokens or 0
        cache_read += record.cache_read_input_tokens or 0
        cache_creation += record.cache_creation_input_tokens or 0
        last_outcome, last_error = outcome, record.error_detail
        if outcome is not InvocationOutcome.SCHEMA_INVALID and parsed is None:
            break

    # **호출 자체가 실패했으면 예외를 전파한다** (2026-08-22 정정). `pipeline/obligations.py`
    # 와 같은 이유이며 같은 사건에서 드러났다 — 초안 호출이 실패했는데 빈 초안이 흘러가면
    # 결과는 `INSUFFICIENT_EVIDENCE` 이고, 그것은 "근거를 찾았는데 부족하다"와 구별되지
    # 않는다. 거부·스키마 위반은 모델의 응답이므로 그대로 둔다(빈 초안 → 이관).
    if parsed is None and last_outcome is InvocationOutcome.ERROR:
        msg = f"영향평가 초안 호출이 실패했다 ({attempt}회 시도): {last_error}"
        raise LLMError(msg)

    failed = parsed is None
    draft = parsed or _abandoned_draft(
        f"모델 응답이 {attempt}회 시도에서 스키마를 만족하지 않았거나 호출이 실패했다"
    )

    consistency = check_draft(draft, obligation_count=len(ctx.obligation_rows))
    if consistency.violations:
        logger.warning(
            "정합성 위반 %d건 폐기: %s",
            len(consistency.violations),
            ", ".join(v.rule.value for v in consistency.violations),
        )
    draft = dataclasses.replace(draft, impacts=consistency.kept)

    gate = enforce_draft_citations(
        draft, retrieved=ctx.retrieved, searched_scope=ctx.retrieval.searched_scope
    )
    if gate.discarded:
        logger.warning(
            "초안 인용 %d건 폐기: %s",
            len(gate.discarded),
            ", ".join(sorted({d.reason.value for d in gate.discarded})),
        )
    draft = dataclasses.replace(draft, impacts=gate.impacts, departments=gate.departments)

    return DraftStep(
        draft=draft,
        raw_output=raw_output,
        consistency=consistency,
        gate=gate,
        attempts=attempt,
        invocation_ids=tuple(invocation_ids),
        injection_signals=signals,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        failed=failed,
    )


async def verify_draft(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    ctx: DraftContext,
    draft: ImpactDraft,
    llm: LLMClient,
    store: DocumentStore,
    revision: int = 0,
    mode: GroundingMode = GroundingMode.ANCHORED,
) -> VerificationRun:
    """초안의 주장들을 하나씩 **독립 컨텍스트로** 판정한다 (gate 3단).

    목적:
        영향 주장과 부서 배정 근거 각각에 대해 "이 인용이 이 주장을 뒷받침하는가"를 묻는다.

    구현 이유:
        검증기 객체를 여기서 만들고 초안을 넘기지 않는다. 넘길 수단이 없어야 원칙 3 이
        코드 구조로 지켜진다 (`pipeline/grounding.py`).

        주장 키를 `impact:{i}` / `dept:{i}` 로 둔다. 재작성 대상과 제거 대상을 이 키로
        지목하므로, 키가 초안의 순서에 묶여 있어야 한다 — 초안이 바뀌면 키도 다시 매겨진다.

        **`mode` 로 검증기를 갈아 끼운다.** 두 구현체의 `verify()` 서명이 같으므로 이 함수의
        나머지는 그대로다 — 대조가 "같은 자리에서 구현만 바꾸기"가 되어야 무엇이 원인인지
        흐려지지 않는다.

    트레이드오프:
        주장마다 호출이 하나다. 묶어 보내면 싸지지만 앞 판정이 뒤 판정을 끌어당기고,
        어느 주장이 왜 떨어졌는지가 흐려진다.

    엣지 케이스:
        - 주장이 0건: 빈 결과를 돌려준다. 판정할 것이 없는 것이지 실패가 아니다.
        - 호출 실패: 예외를 전파한다. **실패는 통과가 아니다.**
        - `LLM_ENABLED` 가 꺼짐: `KillSwitchError`. **검증만 꺼진 상태를 만들지 않는다** —
          초안은 나왔는데 검증이 멈추면 검증되지 않은 초안이 사람에게 갈 수 있다.
    """
    await switches.require(Switch.LLM)
    shared: dict[str, Any] = {
        "conn": conn,
        "llm": llm,
        "store": store,
        "impact_assessment_id": ctx.assessment_id,
        "chunk_ids": ctx.chunk_ids,
        "document_versions": ctx.document_versions,
        "revision": revision,
    }
    verifier: RecordingSupportVerifier | DeAnchoredSupportVerifier = (
        DeAnchoredSupportVerifier(amendment=ctx.after_text, **shared)
        if mode is GroundingMode.DE_ANCHORED
        else RecordingSupportVerifier(**shared)
    )
    specs = ctx.specs
    judgments: list[ClaimJudgment] = []

    for index, impact in enumerate(draft.impacts):
        verdict = await verifier.verify(
            claim=impact.claim,
            quote=impact.quote,
            spec=specs.get(impact.paragraph_id, impact.paragraph_id),
        )
        judgments.append(
            ClaimJudgment(
                key=f"impact:{index}",
                level=verdict.level,
                reason=verdict.reason,
                relation=verdict.relation,
            )
        )

    for index, entry in enumerate(draft.departments):
        verdict = await verifier.verify(
            claim=f"{entry.department}가 관여한다: {entry.rationale}",
            quote=entry.basis_quote,
            spec=specs.get(entry.basis_paragraph_id, entry.basis_paragraph_id),
        )
        judgments.append(
            ClaimJudgment(
                key=f"dept:{index}",
                level=verdict.level,
                reason=verdict.reason,
                relation=verdict.relation,
            )
        )

    return VerificationRun(
        grounding=decide(judgments),
        invocation_ids=tuple(verifier.invocation_ids),
        input_tokens=verifier.input_tokens,
        output_tokens=verifier.output_tokens,
        cache_read_tokens=verifier.cache_read_tokens,
        cache_creation_tokens=verifier.cache_creation_tokens,
        blind_calls=getattr(verifier, "blind_calls", 0),
        blind_cache_hits=getattr(verifier, "blind_cache_hits", 0),
    )


def finalize(
    draft: ImpactDraft, grounding: GroundingResult, *, draft_status: DraftStatus
) -> tuple[ImpactDraft, AssessmentStatus]:
    """뒷받침되지 않은 주장을 제거하고 최종 상태를 정한다. 정하는 것은 코드다.

    목적:
        gate 3단의 판정을 초안에 반영하고, 모델의 `status` 가 아니라 남은 근거로 상태를
        결정한다.

    구현 이유:
        `PARTIAL` 은 제거하지 않는다. 부분 일치는 날조가 아니며 담당자가 보고 판단할
        값이다. 다만 `SUPPORTED` 로 세지도 않는다 (`SupportVerdict.supported`).

    트레이드오프:
        제거된 주장이 초안에서 사라진다. 무엇이 제거됐는지는 `GroundingResult` 에 남으므로
        검토 화면이 함께 보여줄 수 있다 — 결과 객체 둘을 함께 봐야 한다는 부담이 있다.

    엣지 케이스:
        - 영향 문단이 0건이 됨: `INSUFFICIENT_EVIDENCE`. 모델이 `DRAFT` 라고 말해도 그렇다.
        - 제거가 있었으나 남은 것이 있음: `NEEDS_REVIEW`.
    """
    dropped = set(grounding.unsupported_keys)
    if dropped:
        draft = dataclasses.replace(
            draft,
            impacts=tuple(
                impact
                for index, impact in enumerate(draft.impacts)
                if f"impact:{index}" not in dropped
            ),
            departments=tuple(
                entry
                for index, entry in enumerate(draft.departments)
                if f"dept:{index}" not in dropped
            ),
        )

    if not draft.impacts:
        # 영향 문단이 하나도 없으면 부서만 남기지 않는다 — gate 2단
        # (`enforce_draft_citations`)이 같은 규칙을 이미 쓴다. 여기 없으면 **"모른다"고
        # 판정한 평가가 부서 목록을 달고 나가고**, 그것은 절반짜리 평가다.
        # de-anchored 대조에서 이 상태가 실제로 나왔다 (case-005): 영향 주장은 전부
        # 떨어졌는데 부서 배정은 살아남았다.
        return dataclasses.replace(draft, departments=()), AssessmentStatus.INSUFFICIENT_EVIDENCE
    if dropped or draft_status is DraftStatus.NEEDS_REVIEW:
        return draft, AssessmentStatus.NEEDS_REVIEW
    return draft, AssessmentStatus.OK


async def assess_impact(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    ctx: DraftContext,
    llm: LLMClient,
    store: DocumentStore,
    mode: GroundingMode = GroundingMode.ANCHORED,
) -> ImpactOutcome:
    """초안 → 검증 → (재작성 1회) → 최종 판정을 순서대로 수행한다.

    목적:
        LangGraph 없이도 같은 순서가 돌게 한다. 그래프는 이 함수를 부르지 않고 같은
        단계 함수들을 노드로 배치한다 — **두 경로가 같은 부품을 쓴다.**

    구현 이유:
        평가·테스트가 그래프를 거치지 않고 파이프라인만 돌릴 수 있어야 한다. 그래프를
        거쳐야만 측정할 수 있으면 프레임워크 문제와 로직 문제를 구별할 수 없다
        (4단계 지시 §1 과 같은 이유).

    트레이드오프:
        같은 순서가 두 곳(이 함수와 그래프)에 표현된다. 순서가 어긋날 수 있으므로 통합
        테스트가 두 경로의 결과를 같은 기준으로 확인한다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    revision = 0
    previous_raw: str | None = None
    notes: tuple[str, ...] = ()
    invocation_ids: list[UUID] = []
    signals: set[str] = set()
    attempts = 0
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0
    blind_calls = 0
    blind_cache_hits = 0
    verification_error: str | None = None

    while True:
        step = await draft_once(
            conn,
            switches=switches,
            ctx=ctx,
            llm=llm,
            store=store,
            revision=revision,
            previous_raw=previous_raw,
            unsupported_notes=notes,
        )
        invocation_ids.extend(step.invocation_ids)
        signals |= set(step.injection_signals)
        attempts += step.attempts
        input_tokens += step.input_tokens
        output_tokens += step.output_tokens
        cache_read += step.cache_read_tokens
        cache_creation += step.cache_creation_tokens
        previous_raw = step.raw_output

        if step.failed:
            grounding = decide(())
            break

        try:
            run = await verify_draft(
                conn,
                switches=switches,
                ctx=ctx,
                draft=step.draft,
                llm=llm,
                store=store,
                revision=revision,
                mode=mode,
            )
        except LLMError as exc:
            # 삼키지 않는다 — 사유를 결과에 담고 로그에 남긴 뒤 이관한다.
            verification_error = f"{type(exc).__name__}: {exc}"
            logger.exception("gate 3단 검증이 실패했다. 이 평가는 이관한다: %s", verification_error)
            step = dataclasses.replace(
                step, draft=dataclasses.replace(step.draft, impacts=(), departments=())
            )
            grounding = decide(())
            break

        grounding = run.grounding
        invocation_ids.extend(run.invocation_ids)
        input_tokens += run.input_tokens
        output_tokens += run.output_tokens
        cache_read += run.cache_read_tokens
        cache_creation += run.cache_creation_tokens
        blind_calls += run.blind_calls
        blind_cache_hits += run.blind_cache_hits

        if not grounding.needs_rewrite or revision >= MAX_REVISIONS:
            break

        notes = grounding.unsupported_notes
        revision += 1
        logger.info(
            "뒷받침되지 않은 주장 %d건 — 재작성 %d회차 (상한 %d)",
            len(grounding.unsupported_keys),
            revision,
            MAX_REVISIONS,
        )

    final_draft, status = finalize(step.draft, grounding, draft_status=step.draft.status)
    logger.info(
        "영향평가 %s: %s (영향 %d건 / 부서 %d건 / 재작성 %d회 / 판정 %s)",
        ctx.assessment_id,
        status.value,
        len(final_draft.impacts),
        len(final_draft.departments),
        revision,
        {level.value: count for level, count in grounding.counts.items()},
    )

    return ImpactOutcome(
        assessment_id=ctx.assessment_id,
        status=status,
        draft=final_draft,
        consistency=step.consistency,
        gate=step.gate,
        grounding=grounding,
        revisions=revision,
        attempts=attempts,
        invocation_ids=tuple(invocation_ids),
        injection_signals=tuple(sorted(signals)),
        input_tokens_total=input_tokens,
        output_tokens_total=output_tokens,
        cache_read_tokens_total=cache_read,
        cache_creation_tokens_total=cache_creation,
        blind_calls=blind_calls,
        blind_cache_hits=blind_cache_hits,
        verification_error=verification_error,
    )


def _promotion_note(chunk: Any) -> str | None:
    """승격 문단에 붙일 표시. 승격이 아니면 None 이며 프롬프트에 태그가 붙지 않는다."""
    if chunk.source is not RetrievalSource.DELEGATION_PROMOTED or chunk.promotion is None:
        return None
    basis = chunk.promotion
    return f"{basis.via_doc_id} 제{basis.via_article_no}조에서 위임"


def _abandoned_draft(reason: str) -> ImpactDraft:
    """호출이 실패했을 때의 빈 초안. 답을 만들어내지 않고 이관한다."""
    return ImpactDraft(
        status=DraftStatus.INSUFFICIENT_EVIDENCE,
        obligation_type=ObligationType.EDITORIAL,
        risk_level=RiskLevel.LOW,
        risk_reason="평가를 만들지 못했으므로 위험도를 판단하지 않았다",
        confidence=Confidence.LOW,
        summary="",
        reason=reason,
        impacts=(),
        departments=(),
        required_evidence=(),
    )


async def _attempt(
    *,
    llm: LLMClient,
    user_content: str,
    attempt: int,
    revision: int,
    ctx: DraftContext,
    signals: tuple[str, ...],
) -> tuple[InvocationOutcome, ImpactDraft | None, str | None, InvocationRecord]:
    """초안 생성 호출 1회를 시도하고 (결과, 초안, 원본, 기록할 행)을 돌려준다.

    목적:
        성공·스키마 위반·거부·오류를 같은 형태로 다뤄 **어느 경우에도 기록이 남게** 한다.

    구현 이유:
        3단계 `pipeline/obligations.py` 의 `_attempt` 와 같은 구조다. 하나로 합치려면
        프롬프트·스키마·파서를 인자로 받는 일반 함수가 되고, 그 함수는 어느 단계의
        기록인지 모르게 된다. **기록의 의미를 아는 코드가 기록을 만든다.**

    트레이드오프:
        반환 튜플이 넷이라 읽기 불편하다. 기록을 빠뜨릴 수 없게 만드는 것이 목적이다.

    엣지 케이스:
        - `SchemaViolationError`: 원본이 없을 수 있다(파싱 이전 실패).
        - 파싱은 됐으나 enum 밖: 스키마 위반으로 취급하되 예외 이름을 남겨 원인을 가른다.
    """
    base: dict[str, Any] = {
        "purpose": InvocationPurpose.IMPACT_ASSESSMENT,
        "prompt_template_id": PROMPT.id,
        "prompt_template_sha256": PROMPT.sha256,
        "model_id": llm.model_id,
        "attempt": attempt,
        "revision": revision,
        "impact_assessment_id": ctx.assessment_id,
        "input_document_versions": ctx.document_versions,
        "retrieved_chunk_ids": ctx.chunk_ids,
        "retrieval_mode": ctx.retrieval.mode.value,
        "injection_signals": signals,
    }

    try:
        result = await llm.complete(
            prompt_id=PROMPT.id,
            prompt_version=PROMPT.version,
            system=PROMPT.system,
            user_content=user_content,
            response_schema=IMPACT_SCHEMA,
        )
    except SchemaViolationError as exc:
        return (
            InvocationOutcome.SCHEMA_INVALID,
            None,
            None,
            InvocationRecord(
                api_version="unknown",
                request_params={"note": "스키마 위반으로 응답을 폐기했다"},
                outcome=InvocationOutcome.SCHEMA_INVALID,
                error_detail=str(exc),
                **base,
            ),
        )
    except LLMError as exc:
        outcome = (
            InvocationOutcome.REFUSAL
            if type(exc).__name__ == "RefusalError"
            else InvocationOutcome.ERROR
        )
        return (
            outcome,
            None,
            None,
            InvocationRecord(
                api_version="unknown",
                request_params={"note": "호출이 실패했다"},
                outcome=outcome,
                error_detail=str(exc),
                **base,
            ),
        )

    try:
        parsed = parse_draft(result.output)
    except (ValueError, TypeError, KeyError) as exc:
        return (
            InvocationOutcome.SCHEMA_INVALID,
            None,
            result.raw_text,
            InvocationRecord(
                api_version=result.api_version,
                request_params=result.request_params,
                outcome=InvocationOutcome.SCHEMA_INVALID,
                raw_output=result.raw_text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                # 스키마를 어긴 응답도 토큰은 청구된다. 캐시 토큰을 여기서 빼면
                # **실패한 시도의 비용이 기록에서 사라진다.**
                cache_read_input_tokens=result.cache_read_input_tokens,
                cache_creation_input_tokens=result.cache_creation_input_tokens,
                latency_ms=result.latency_ms,
                error_detail=f"{type(exc).__name__}: {exc}",
                **base,
            ),
        )

    return (
        InvocationOutcome.OK,
        parsed,
        result.raw_text,
        InvocationRecord(
            api_version=result.api_version,
            request_params=result.request_params,
            outcome=InvocationOutcome.OK,
            raw_output=result.raw_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_input_tokens=result.cache_read_input_tokens,
            cache_creation_input_tokens=result.cache_creation_input_tokens,
            latency_ms=result.latency_ms,
            **base,
        ),
    )
