"""사내 내부통제 규정에서 영향 후보 문단을 검색하는 패키지.

목적:
    조문 변경 내용을 질의로 삼아, 사내 규정 문서에서 영향받을 수 있는 문단을
    문단 ID와 함께 반환한다. 이 패키지가 반환한 문단 ID 집합이 이후 인용 검증의
    유일한 정답 집합이 된다.

구현 이유:
    검색 단위를 문서가 아니라 문단으로 잡는다. 담당자가 실제로 수정하는 단위가
    문단이며, 문서 단위로 제안하면 "이 문서 어딘가"라는 확인 불가능한 출력이
    된다. 반환값에 문단 ID를 필수로 포함시키는 것은 원칙 2를 성립시키기 위한
    전제다. ID가 없으면 검증할 대상이 없다.

트레이드오프:
    문단 단위 분할은 문맥을 잘라내므로, 앞 문단의 정의에 의존하는 문단은 단독
    검색 시 점수가 낮아진다. 정밀한 인용 가능성을 얻는 대신 재현율 일부를
    포기했다. 손실은 상위 문단 문맥을 함께 임베딩하는 방식으로 완화하되,
    반환 단위는 문단으로 유지한다.

엣지 케이스:
    - 관련 문단이 없는 경우: 빈 결과를 그대로 반환한다. 유사도 임계값을 낮춰
      억지로 채우지 않는다. 빈 결과는 "영향 없음" 판정의 정당한 근거다.
    - RETRIEVAL_ENABLED 가 꺼진 경우: 빈 결과가 아니라 **예외로 멈춘다**
      (`guards.killswitch.KillSwitchError`, 5단계). 둘을 구별하지 않으면 킬 스위치가
      "영향 없음"으로 오독된다 — 빈 결과는 이 패키지에서 **정당한 판정 근거**이므로
      비활성이 같은 값을 쓰면 안 된다.
    - 문단 ID 중복·소실: 검색 결과의 ID는 검증 단계의 정답 집합이므로,
      중복이나 누락은 조용히 넘기지 않고 오류로 올린다.
"""

from regchange.retrieval.corpus import (
    CorpusError,
    load_corpus,
    parse_article_spec,
    parse_policy_document,
)
from regchange.retrieval.delegation import (
    DelegationEdge,
    DelegationError,
    DelegationGraph,
    DelegationSource,
    build_delegation_graph,
    parse_delegations,
)
from regchange.retrieval.models import (
    DelegationReport,
    PolicyArticle,
    PolicyDocument,
    PromotionBasis,
    PromotionMechanism,
    RetrievalResult,
    RetrievalSource,
    RetrievedChunk,
    SearchMode,
)
from regchange.retrieval.promote import load_delegation_graph, promote_by_delegation
from regchange.retrieval.query import build_query
from regchange.retrieval.search import SearchError, search

__all__ = [
    "CorpusError",
    "DelegationEdge",
    "DelegationError",
    "DelegationGraph",
    "DelegationReport",
    "DelegationSource",
    "PolicyArticle",
    "PolicyDocument",
    "PromotionBasis",
    "PromotionMechanism",
    "RetrievalResult",
    "RetrievalSource",
    "RetrievedChunk",
    "SearchError",
    "SearchMode",
    "build_delegation_graph",
    "build_query",
    "load_corpus",
    "load_delegation_graph",
    "parse_article_spec",
    "parse_delegations",
    "parse_policy_document",
    "promote_by_delegation",
    "search",
]
