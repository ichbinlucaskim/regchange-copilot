"""변경 유형 판정 — 결정론적 순수 함수 (원칙 1).

목적:
    같은 법령의 두 버전을 조문 단위로 비교해 ADDED / DELETED / MODIFIED /
    EDITORIAL / UNCHANGED 를 판정한다.

구현 이유:
    LLM 을 쓰지 않는다. "무엇이 바뀌었나"는 사실 확인이지 추론이 아니며, 원문 두
    버전을 비교하는 일에 LLM 을 넣으면 정확도는 떨어지고 비용과 비결정성만 는다
    (원칙 1). 같은 입력에 항상 같은 출력을 낸다.

    **짝짓기는 `(article_no, branch_no)` 로 한다.** 문자열 `"제5조의2"` 로 짝지으면
    렌더링 규칙이 바뀔 때 짝짓기가 함께 흔들린다 — 저장은 구조로 하고 문자열은
    렌더링 결과라는 ADR-001 의 결정이 여기까지 이어진다.

    **비교 대상은 조립본(`body_norm_sha256`)이다.** `조문내용` 만 비교하면 항이
    있는 조문에서 제목 줄만 비교하게 된다(edge-case #5). 특금법 2011↔2020 실측에서
    그 기준은 놓친 변경 5건, 조립본 기준은 0건이다.

    **EDITORIAL 을 별도 유형으로 둔다.** 조립본 해시가 같은데 마커만 다른 경우이며,
    이것이 R-14 의 유일한 방어선이다. 마커를 보존한 ADR-002 의 결정이 여기서 값을
    한다 — 마커를 버렸다면 EDITORIAL 과 실질 무변경을 구별할 수 없다.

트레이드오프:
    조문 전체가 재작성된 경우(전부개정) `(article_no, branch_no)` 짝짓기는 그것을
    MODIFIED 로 본다. 의미적 연속성 판단을 포기한 대신 결정론성과 재현성을 얻었다.
    재현성이 감사 요구사항이므로 이 교환은 의도적이다.

    번호가 밀리는 블록 재번호(제6조→제9조)에서는 같은 번호의 서로 다른 조문이
    MODIFIED 로 잡힌다. 이동 후보가 그 사실을 별도로 제시하며, 두 신호를 합쳐
    해석하는 것은 검토자의 몫이다 — 시스템이 합쳐서 확정하지 않는다 (ADR-003).

엣지 케이스:
    - 같은 `(article_no, branch_no)` 가 한 버전에 두 번: 유니크 제약이 막으므로
      발생하지 않는다. 발생하면 `DiffError` 로 드러낸다 — 조용히 덮어쓰지 않는다.
    - `unit_type='HEADING'`: 비교 대상이 아니다. 호출자가 걸러서 넘긴다 (ADR-001).
    - 마커 순서만 다른 경우: 순서를 유지해 비교하므로 EDITORIAL 로 잡힌다. 어느
      계층에 몇 개 붙었는지가 신호이므로 집합으로 뭉개지 않는다.
    - 양쪽 모두 비어 있는 조문: 해시가 같으므로 UNCHANGED 다.
"""

from __future__ import annotations

from collections.abc import Iterable

from regchange.diff.models import (
    DEFAULT_PRIORITY_RANK,
    EDITORIAL_PRIORITY_RANK,
    ArticleChange,
    ArticleSnapshot,
    ChangeType,
    DiffCounts,
    DiffError,
)


def index_by_ref(articles: Iterable[ArticleSnapshot]) -> dict[tuple[int, int], ArticleSnapshot]:
    """`(article_no, branch_no)` 로 색인한다. 중복이 있으면 실패시킨다.

    목적:
        짝짓기의 유일한 진입점을 만든다.

    구현 이유:
        중복을 조용히 덮어쓰면 조문 하나가 사라진다. **이 저장소는 그 사고를 겪었다** —
        집계 스크립트의 dict 붕괴로 `0013001` 이 소실돼 "벌칙 1:2"로 잘못 세었고,
        그 숫자가 ADR-003 의 근거로 쓰였다. 실제로는 2:2 다
        (silent-undercounting.md 사건 1).

        그래서 dict 로 색인하되 **덮어쓰기를 검사한다.** dict 를 쓰지 않는 것이
        아니라, dict 가 조용히 삼키는 순간을 잡는다.

    트레이드오프:
        검사 때문에 색인이 조금 느려진다. 조문 수가 최대 682건이므로 무시할 수 있다.

    엣지 케이스:
        - 중복 좌표: `DiffError`. DB 유니크 제약이 이미 막지만, 파서 결과를 직접
          넘기는 경로에서는 제약이 없다.
    """
    indexed: dict[tuple[int, int], ArticleSnapshot] = {}
    for article in articles:
        if article.ref in indexed:
            raise DiffError(
                f"조문 좌표가 중복된다: 제{article.ref[0]}조"
                f"{'의' + str(article.ref[1]) if article.ref[1] else ''}. "
                "덮어쓰면 조문이 조용히 사라진다 (사건 1)"
            )
        indexed[article.ref] = article
    return indexed


def classify(before: ArticleSnapshot, after: ArticleSnapshot) -> ChangeType:
    """짝지어진 두 조문의 변경 유형을 판정한다.

    목적:
        실질 변경(MODIFIED)과 문구정비(EDITORIAL)와 무변경을 가른다.

    구현 이유:
        조립본 해시를 먼저 본다. 해시가 다르면 본문이 다르므로 마커를 볼 필요가
        없다. 해시가 같을 때만 마커를 비교하며, 그때 다른 것이 EDITORIAL 이다.

        순서를 이렇게 두는 이유: 마커를 먼저 보면 "본문도 바뀌고 마커도 바뀐"
        경우가 EDITORIAL 로 분류될 수 있다. 실질 변경이 문구정비로 강등되면
        담당자가 봐야 할 것을 최하위에서 보게 된다 — R-14 를 막으려다 반대 방향의
        실패를 만드는 셈이다.

    트레이드오프:
        마커가 본문에서 분리된 값이라는 전제에 의존한다(ADR-002). 정규화 규칙이
        바뀌어 마커가 `body_norm` 에 남으면 EDITORIAL 이 영영 발생하지 않는다.
        `rule_version` 을 함께 저장하는 이유가 그 변화를 사후에 알기 위해서다.

    엣지 케이스:
        - 해시 같음 + 마커 같음 → UNCHANGED
        - 해시 같음 + 마커 다름 → EDITORIAL
        - 해시 다름 → MODIFIED (마커 상태와 무관)
    """
    if before.body_norm_sha256 != after.body_norm_sha256:
        return ChangeType.MODIFIED
    if before.marker_signature != after.marker_signature:
        return ChangeType.EDITORIAL
    return ChangeType.UNCHANGED


def priority_rank_for(change_type: ChangeType) -> int:
    """변경 유형의 우선순위 등급. EDITORIAL 만 최하위로 내린다."""
    return EDITORIAL_PRIORITY_RANK if change_type is ChangeType.EDITORIAL else DEFAULT_PRIORITY_RANK


def diff_articles(
    from_articles: Iterable[ArticleSnapshot],
    to_articles: Iterable[ArticleSnapshot],
) -> tuple[tuple[ArticleChange, ...], DiffCounts]:
    """두 버전의 조문 집합을 비교해 변경 목록과 건수를 낸다.

    목적:
        `change_set` 하나가 담을 변경 전체를 만든다.

    구현 이유:
        결과를 **정렬된 리스트**로 만든다. 좌표 순서로 고정하면 같은 입력에 항상
        같은 순서의 출력이 나오고, 감사 재현에서 두 실행의 결과를 그대로 대조할
        수 있다. 집합으로 두면 순서가 실행마다 달라져 대조가 어려워진다.

        UNCHANGED 는 `changes` 에 담지 않고 건수로만 센다. 변경 없는 조문까지
        행으로 만들면 `article_change` 가 조문 전수의 사본이 된다. 다만 **건수에는
        반드시 포함한다** — 빼면 조문 개수 보존 단언이 성립하지 않는다.

    트레이드오프:
        UNCHANGED 행이 없으므로 "이 조문을 검사했는가"를 행으로 증명할 수 없다.
        대신 `from_article_count`/`to_article_count` 와 건수 CHECK 가 그것을
        증명한다 — 전수를 검사했다는 사실은 합이 맞는 것으로 드러난다.

    엣지 케이스:
        - 한쪽이 비어 있음: 전부 ADDED 또는 전부 DELETED 가 된다. 조문 0건 문서는
          파서가 이미 막으므로(ADR-005) 정상 경로에서는 오지 않는다.
        - 좌표 중복: `index_by_ref` 가 실패시킨다.
    """
    before = index_by_ref(from_articles)
    after = index_by_ref(to_articles)

    changes: list[ArticleChange] = []
    added = deleted = modified = editorial = unchanged = 0

    for ref in sorted(set(before) | set(after)):
        source = before.get(ref)
        target = after.get(ref)

        if source is None and target is not None:
            added += 1
            change_type = ChangeType.ADDED
        elif target is None and source is not None:
            deleted += 1
            change_type = ChangeType.DELETED
        elif source is not None and target is not None:
            change_type = classify(source, target)
            if change_type is ChangeType.MODIFIED:
                modified += 1
            elif change_type is ChangeType.EDITORIAL:
                editorial += 1
            else:
                unchanged += 1
                continue
        else:  # pragma: no cover — 합집합에서 온 좌표라 둘 다 None 일 수 없다
            raise DiffError(f"좌표 {ref} 가 양쪽 모두에 없다")

        changes.append(
            ArticleChange(
                change_type=change_type,
                article_no=ref[0],
                branch_no=ref[1],
                from_article_id=source.article_id if source else None,
                to_article_id=target.article_id if target else None,
                priority_rank=priority_rank_for(change_type),
            )
        )

    counts = DiffCounts(
        from_article_count=len(before),
        to_article_count=len(after),
        added=added,
        deleted=deleted,
        modified=modified,
        editorial=editorial,
        unchanged=unchanged,
    )
    counts.verify(context="diff_articles")
    return tuple(changes), counts
