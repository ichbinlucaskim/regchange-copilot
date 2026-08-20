"""법령 XML 응답을 조문 트리로 파싱하는 패키지.

목적:
    법제처 `target=law` / `target=eflaw` 본문 응답을 받아 조문·항·호·목 트리와
    정규화 텍스트, 개정 마커, 조문참고자료의 구조화된 참조를 산출한다.

구현 이유:
    파이프라인에서 이 패키지를 가장 먼저 만든다. `조문키` 재구성이라는 **정답이
    이미 픽스처에 있어** 네트워크 없이 즉시 채점되기 때문이다. API 클라이언트를
    먼저 만들면 결과가 틀렸을 때 파서 탓인지 API 탓인지 구별할 수 없다.

트레이드오프:
    XML 응답 구조에 강하게 결합된다. 법제처가 스키마를 바꾸면 이 패키지를 고쳐야
    한다. 범용 추상화를 포기한 대신, 실측으로 확인된 구조에 정확히 맞췄고 그
    구조가 깨지면 조용히 잘못된 트리를 만드는 대신 예외로 드러난다.

엣지 케이스:
    - `<목>`은 `<호>`의 자식이 아니라 `<항>`의 자식이며, 문서 순서로 직전 `<호>`에
      귀속된다. 선행 호 없이 목이 나오면 **예외를 던진다** — 조용히 넘기면 파서
      버그가 숨는다 (edge-case #4).
    - 순서를 잃는 자료구조를 쓰지 않는다. dict 키로 조문을 모으면 같은 제목·같은
      키가 서로를 덮어쓴다. 이 저장소에서 실제로 발생한 사고다 (ADR-003 정정 이력).
    - `조문내용`은 `<항>` 유무로 의미가 달라진다. 항이 있으면 제목 줄만, 없으면
      조문 전문이 들어간다 (edge-case #5).
    - `조문키`는 유일하지 않다. 편장절관 제목행이 뒤따르는 조문의 키를 공유하므로
      `seq_in_doc`으로 구분한다 (ADR-001, edge-case #2).
    - 행정규칙(`AdmRulService`)은 파싱하지 않는다 (ADR-006, 3단계).
"""

from regchange.parse.assemble import assemble_body
from regchange.parse.law_xml import ParseError, parse_law_document
from regchange.parse.models import (
    AmendmentMarker,
    ArticleRef,
    ArticleUnit,
    Hang,
    Ho,
    LawDocument,
    Mok,
    MoveReference,
    NormalizedText,
    UnitType,
)
from regchange.parse.normalize import NORM_RULE_VERSION, normalize

__all__ = [
    "NORM_RULE_VERSION",
    "AmendmentMarker",
    "ArticleRef",
    "ArticleUnit",
    "Hang",
    "Ho",
    "LawDocument",
    "Mok",
    "MoveReference",
    "NormalizedText",
    "ParseError",
    "UnitType",
    "assemble_body",
    "normalize",
    "parse_law_document",
]
