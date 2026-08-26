"""검색 계층의 값 모델 — 문서, 조 단위 문단, 검색 결과.

목적:
    사내 규정 문서와 그 조 단위 문단, 그리고 검색이 돌려주는 후보를 타입이 붙은
    불변 값으로 표현한다. 적재·검색·채점이 이 모델만 보고 동작한다.

구현 이유:
    `RetrievedChunk` 에 `paragraph_id` 를 필수로 둔다. 원칙 2가 성립하려면 검색
    결과에 **검증 가능한 식별자**가 반드시 있어야 하고, 없으면 인용 검증의 정답
    집합 자체가 만들어지지 않는다. 점수를 optional 로 두지 않는 것도 같은 이유다 —
    "왜 이것이 올라왔는가"를 사후에 설명할 수 없는 결과를 만들지 않는다.

    `RetrievedChunk` 에 `source`(PRIMARY / DELEGATION_PROMOTED)를 둔다. 후보가 **어떻게**
    올라왔는지를 값으로 남기지 않으면, 위임 승격(R-22)이 재현율을 올렸는지 정밀도를
    깎았는지 사후에 분리할 수 없고 검토 화면도 "왜 이게 여기 있나"에 답하지 못한다.
    기본값을 `PRIMARY` 로 두어 승격을 쓰지 않는 기존 경로의 의미가 바뀌지 않게 했다.

    `parse.models` 의 `Frozen` 관례를 따라 전부 불변으로 둔다. 검색 결과가 호출부에서
    변형되면 `llm_invocation.retrieved_chunk_ids` 에 기록한 집합과 실제로 프롬프트에
    들어간 집합이 달라질 수 있고, 그 어긋남은 아무 오류도 내지 않는다.

트레이드오프:
    `PolicyArticle` 이 `text_raw` 와 `text_norm` 을 모두 들고 다녀 메모리가 두 배다.
    152조 규모에서는 무의미한 비용이고, 인용은 원문을 가리켜야 하며 검색은 정규화본을
    써야 하므로(ADR-002) 둘 중 하나를 버릴 수 없다.

    검색 결과에 문서 메타데이터(제목·버전)를 복제해 담는다. 정규화 관점에서는
    중복이지만, `INSUFFICIENT_EVIDENCE` 의 `searched_scope` 가 "어느 문서 어느
    버전까지 찾아봤는가"를 요구하므로(3단계 §6) 결과만으로 그 답이 나와야 한다.

엣지 케이스:
    - `article_title` 이 빈 문자열: 파서가 거부하므로 여기까지 오지 않는다.
      그럼에도 타입을 Optional 로 두지 않는다 — 없을 수 없는 값을 있을 수 있는 것처럼
      선언하면 호출부마다 무의미한 분기가 생긴다.
    - 같은 문단이 두 번 담긴 결과: 이 모델은 막지 않는다. 중복 검사는 검색 함수의
      책임이며, 거기서 오류로 올린다 (`retrieval/__init__.py` 엣지 케이스).
    - `score` 가 검색 방식마다 다른 척도를 갖는다. 방식 간 점수를 직접 비교하지
      않으며, 결합은 순위 기반(RRF)으로만 한다.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    """불변 모델 공통 설정."""

    model_config = ConfigDict(frozen=True)


class SearchMode(StrEnum):
    """검색 방식. 세 가지를 같은 골든셋으로 비교해 결합 방식을 결정한다."""

    VECTOR = "VECTOR"
    """임베딩 코사인 거리."""
    LEXICAL = "LEXICAL"
    """문자 bigram BM25. 한국어는 조사가 붙어 어절 단위 색인이 약하다."""
    HYBRID = "HYBRID"
    """위 둘의 순위를 RRF 로 결합."""


class RetrievalSource(StrEnum):
    """이 문단이 **어떻게 후보가 되었는가**. 근거의 존재만이 아니라 경로도 표시한다.

    ADR-007 의 `article_key_source`, ADR-015 의 임베딩 출처 표시와 같은 계열이다.
    셋 다 같은 요구에서 나온다 — 값을 믿을지 판단하려면 값이 어디서 왔는지 알아야 한다.

    이 표시가 없으면 세 가지를 할 수 없다: (a) 검토 화면에서 "왜 이게 여기 있나"에 답하기,
    (b) 프롬프트에서 모델이 두 경로를 구별하기, (c) **측정에서 승격이 재현율을 올렸는지
    정밀도를 깎았는지 분리해서 보기.**
    """

    PRIMARY = "PRIMARY"
    """질의로 직접 검색되어 top-k 에 든 문단."""
    DELEGATION_PROMOTED = "DELEGATION_PROMOTED"
    """위임 관계를 타고 올라가 승격된 문단 (R-22, `retrieval/promote.py`)."""


class PromotionMechanism(StrEnum):
    """승격이 어느 경로로 이루어졌는가. 둘의 신뢰도가 다르므로 구별한다."""

    DECLARED_ARTICLE = "DECLARED_ARTICLE"
    """문서가 상위 조를 직접 지목했다(`(ISP-GUIDE-002) 제35조에서 위임된`). 재검색하지 않았다."""
    RESEARCHED = "RESEARCHED"
    """문서 단위 위임만 있어, 상위 문서로 범위를 좁혀 같은 질의를 다시 던졌다."""


class PromotionBasis(Frozen):
    """승격된 문단 하나가 **왜** 올라왔는지에 대한 근거.

    목적:
        검토자와 측정 양쪽이 "이 후보는 검색이 찾은 것이 아니라 위임을 타고 올라온 것"임을
        값으로 확인할 수 있게 한다.

    구현 이유:
        위임 근거 문장(`delegation_quote`)을 담는다. 관계 이름만 남기면 담당자는 그 관계가
        실재하는지 확인하러 문서를 열어야 하고, 열어 봐야 아는 근거는 검증 비용을 사람에게
        전가한다 (gate 2단이 인용문을 함께 대조하는 것과 같은 이유).

    트레이드오프:
        같은 상위 문단이 여러 하위 문단 때문에 올라올 수 있는데 근거를 하나만 담는다.
        **1차 검색에서 가장 높은 순위**의 하위 문단을 담는다 — 전부 담으면 값이 커지고,
        담당자가 실제로 확인하는 것은 가장 강한 근거 하나다.

    엣지 케이스:
        - `mechanism=DECLARED_ARTICLE`: 재검색을 하지 않았으므로 `score` 는 승격 대상
          문단의 검색 점수가 아니다. 그 사실이 `mechanism` 으로 드러난다.
    """

    via_doc_id: str
    """1차 검색에 잡힌 하위 문서."""
    via_article_no: int
    """그 문서에서 가장 높은 순위로 잡힌 조."""
    via_rank: int
    """그 조의 1차 검색 순위."""
    delegation_quote: str
    """위임을 선언한 문장 원문 (`retrieval/delegation.py`)."""
    mechanism: PromotionMechanism


class DelegationReport(Frozen):
    """승격이 실제로 무엇을 했고 무엇을 건너뛰었는지.

    **건너뛴 것을 함께 담는다.** 간선 수만 세면 "원래 몇 개여야 했는가"를 알 수 없고,
    표기가 바뀌어 위임을 못 뽑은 상태가 "승격할 것이 없었다"로 보인다.
    """

    top_n: int
    """문서 단위 위임에서 상위 문서 재검색으로 올린 조의 수."""
    promoted: int
    used_edges: tuple[str, ...]
    """실제로 승격을 일으킨 간선 — `ISP-GUIDE-003 → ISP-POL-001` 표기."""
    declared_article_edges: tuple[str, ...]
    """조가 지목되어 재검색 없이 올린 간선."""
    skipped_dangling: tuple[str, ...]
    """상위 문서를 갖고 있지 않아 건너뛴 간선."""
    undeclared_docs: tuple[str, ...]
    """제1조에 위임 선언이 없던 문서. 최상위 문서에서는 정상이다."""


class PolicyArticle(Frozen):
    """사내 규정 문서의 조 하나. 검색·인용·검증의 공통 단위다."""

    article_no: int
    article_title: str
    text_raw: str
    text_norm: str
    text_norm_sha256: str
    norm_rule_version: str
    seq_in_doc: int

    @property
    def spec(self) -> str:
        """골든셋 `article_spec` 과 같은 표기 — `제18조 (접속기록의 보관)`."""
        return f"제{self.article_no}조 ({self.article_title})"


class PolicyDocument(Frozen):
    """사내 규정 문서 한 버전과 그 조 전체."""

    doc_id: str
    version: str
    title: str
    owner_dept: str
    classification: str
    effective_date: dt.date
    parent_laws: tuple[str, ...]
    revision_history: tuple[dict[str, str], ...]
    source_path: str
    source_sha256: str
    articles: tuple[PolicyArticle, ...]

    @property
    def label(self) -> str:
        """`searched_scope` 에 쓰는 표기 — `ISP-GUIDE-002 v5.1`."""
        return f"{self.doc_id} v{self.version}"

    @property
    def by_article_no(self) -> dict[int, PolicyArticle]:
        """조 번호로 조를 찾는 사전. 골든셋이 조 번호로 가리키므로 대조에 쓴다."""
        return {article.article_no: article for article in self.articles}

    def text_of(self, article_no: int) -> str:
        """조 하나의 제목+본문. 인용·수치 검사와 색인 입력이 보는 것과 같은 텍스트다."""
        article = self.by_article_no[article_no]
        return f"{article.article_title}\n{article.text_raw}"


class RetrievedChunk(Frozen):
    """검색이 돌려준 문단 하나. 이 집합이 인용 검증의 정답 집합이 된다."""

    paragraph_id: UUID
    doc_id: str
    doc_version: str
    article_no: int
    article_title: str
    text_raw: str
    score: float
    rank: int
    source: RetrievalSource = RetrievalSource.PRIMARY
    """이 문단이 어떻게 후보가 되었는가. 기본값이 `PRIMARY` 인 이유는 승격 경로를 거치지
    않은 모든 기존 호출부가 그대로 참이기 때문이다 — 새 필드가 과거 의미를 바꾸지 않는다."""
    promotion: PromotionBasis | None = None
    """승격된 경우에만 채워진다. `source` 와 이 값은 항상 함께 움직인다."""

    @property
    def key(self) -> tuple[str, int]:
        """채점·대조에 쓰는 자연키 — `(doc_id, 조 번호)`."""
        return (self.doc_id, self.article_no)

    @property
    def spec(self) -> str:
        """골든셋 `article_spec` 과 같은 표기."""
        return f"제{self.article_no}조 ({self.article_title})"


class RetrievalResult(Frozen):
    """한 번의 검색 실행 전체. 결과와 함께 '어디까지 찾아봤는가'를 담는다."""

    mode: SearchMode
    as_of: dt.date
    chunks: tuple[RetrievedChunk, ...]
    searched_scope: tuple[str, ...]
    """검색 대상이었던 문서와 버전. `INSUFFICIENT_EVIDENCE` 의 필수 필드다."""
    corpus_size: int
    """`as_of` 시점에 검색 대상이었던 문단 수. 0이면 검색이 아니라 적재를 의심한다."""
    delegation: DelegationReport | None = None
    """위임 승격을 거쳤으면 그 기록. `None` 은 "승격을 시도하지 않았다"이며
    `promoted=0` 인 리포트("시도했으나 올린 것이 없다")와 다른 값이다."""

    @property
    def primary(self) -> tuple[RetrievedChunk, ...]:
        """1차 검색으로 잡힌 문단만. 승격 전후를 비교할 때 쓴다."""
        return tuple(c for c in self.chunks if c.source is RetrievalSource.PRIMARY)

    @property
    def promoted(self) -> tuple[RetrievedChunk, ...]:
        """승격된 문단만. 재현율 상승과 정밀도 하락을 분리해 재는 단위다."""
        return tuple(c for c in self.chunks if c.source is RetrievalSource.DELEGATION_PROMOTED)
