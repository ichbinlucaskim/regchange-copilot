"""파서 결과를 diff 입력으로 좁힌다.

목적:
    `ArticleUnit`(파싱 트리)을 `ArticleSnapshot`(비교에 필요한 값만)으로 바꾼다.

구현 이유:
    변환을 한 곳에 둔다. 파서 결과와 DB 행이라는 두 출처가 같은 스냅샷 형태로
    좁혀져야 두 경로가 같은 판정 코드를 탄다 — 픽스처로 검증한 동작이 운영에서도
    성립한다는 보장이 거기서 나온다.

    조립본은 `parse.assemble.assemble_body()` 를 부른다. diff 가 스스로 조립하면
    규칙이 둘로 갈리고, 검색된 텍스트와 비교된 텍스트가 달라진다.

    마커를 문자열 튜플로 편다. dict 나 모델 그대로 두면 DB 에서 읽은 jsonb 와
    파서 결과의 비교가 타입 차이로 어긋난다. **두 출처가 같은 값을 만들어야
    하므로 가장 단순한 표현으로 내린다.**

트레이드오프:
    마커를 문자열로 내리면 구조 정보가 사라져 "어떤 종류의 마커인지"를 이 값만
    보고는 알 수 없다. EDITORIAL 판정에는 동등성만 필요하므로 충분하며, 종류가
    필요한 계층은 원본 `body_markers` jsonb 를 읽는다.

엣지 케이스:
    - 마커가 없는 조문: 빈 튜플. "마커 없음 vs 마커 없음"은 변경 없음이다.
    - 날짜가 없는 마커(`<단서 생략>`): 날짜 자리를 비운 채로 서명에 포함한다.
      제거하면 단서 생략이 붙고 떨어진 것을 감지하지 못한다.
    - `unit_type='HEADING'`: 변환은 되지만 비교 대상이 아니다. 거르는 것은
      호출자의 책임이다 (ADR-001).
"""

from __future__ import annotations

from uuid import UUID

from regchange.diff.models import ArticleSnapshot
from regchange.parse.assemble import assemble_body
from regchange.parse.models import AmendmentMarker, ArticleUnit


def marker_signature(markers: tuple[AmendmentMarker, ...]) -> tuple[str, ...]:
    """마커를 비교 가능한 문자열 튜플로 편다. 순서를 유지한다."""
    return tuple(
        f"{marker.type.value}|{','.join(d.isoformat() for d in marker.dates)}" for marker in markers
    )


def snapshot_from_unit(unit: ArticleUnit, *, article_id: UUID | None = None) -> ArticleSnapshot:
    """`ArticleUnit` 을 비교용 스냅샷으로 바꾼다.

    목적:
        픽스처 기반 테스트가 DB 없이도 실제 판정 경로를 통과하게 한다.

    구현 이유:
        `article_id` 를 선택 인자로 둔다. 파서 결과에는 DB 대리키가 없고, DB 에서
        읽을 때는 있다. 없으면 None 으로 두고 변경 행의 참조를 비운다 — 없는 키를
        지어내지 않는다.

    트레이드오프:
        조립을 매번 수행하므로 같은 조문을 여러 번 스냅샷으로 만들면 중복 계산이
        난다. 적재 경로는 한 번만 조립해 컬럼에 저장하므로 실제 비용은 테스트에만
        발생한다.

    엣지 케이스:
        - 본문이 빈 조문: 빈 문자열과 그 해시가 담긴다. `is_empty` 가 True 가 되어
          유사도 계산에서 제외된다.
    """
    body = assemble_body(unit)
    return ArticleSnapshot(
        article_id=article_id,
        article_no=unit.article_no,
        branch_no=unit.branch_no,
        title=unit.title,
        body_norm=body.norm,
        body_norm_sha256=body.sha256,
        marker_signature=marker_signature(body.markers),
        moves=unit.moves,
        reference_raw=unit.reference_raw,
    )
