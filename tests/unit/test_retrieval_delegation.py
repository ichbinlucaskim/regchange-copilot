"""위임 관계 파싱 — R-22 승격의 유일한 근거이므로 표기가 흔들리면 조용히 0건이 된다.

이 테스트가 존재하는 이유: 승격은 위임 간선이 있을 때만 일어난다. 간선이 0개면 승격도
0건이고, 그 상태는 "승격할 것이 없었다"와 구별되지 않는다. 실제 코퍼스 5종에서 간선이
4개 나오는 것을 고정해 두면, 표기가 바뀌었을 때 검색 결과가 아니라 여기서 먼저 깨진다.
"""

from __future__ import annotations

import pathlib

import pytest

from regchange.retrieval.corpus import parse_policy_document
from regchange.retrieval.delegation import (
    DELEGATION_ARTICLE_NO,
    DelegationError,
    DelegationSource,
    build_delegation_graph,
    parse_delegations,
)

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "evals" / "corpus" / "internal-policies"


def test_parses_document_level_delegation() -> None:
    """`「문서명」(DOC-ID)에서 위임된` 형태를 문서 단위 간선으로 읽는다."""
    text = "이 지침은 「정보보호정책」(ISP-POL-001)에서 위임된 사항과 세부 기준을 정한다."
    edges = parse_delegations("ISP-GUIDE-002", 1, text)

    assert len(edges) == 1
    assert edges[0].parent_doc_id == "ISP-POL-001"
    assert edges[0].parent_article_no is None
    assert edges[0].source is DelegationSource.PARSED
    assert "위임된" in edges[0].evidence_quote


def test_keeps_declared_article_number() -> None:
    """조가 지목된 위임은 그 조 번호를 버리지 않는다 — 재검색보다 정확한 근거다."""
    text = "이 절차서는 「정보보호 관리지침」(ISP-GUIDE-002) 제35조에서 위임된 사항으로서 …"
    edges = parse_delegations("ISP-PROC-002", 1, text)

    assert edges[0].parent_article_no == 35


def test_no_delegation_is_not_an_error() -> None:
    """위임 선언이 없는 것은 실패가 아니다. 최상위 문서에서는 정상이다."""
    assert parse_delegations("ISP-POL-001", 1, "이 정책은 정보보호의 기본 방향을 정한다.") == ()


def test_graph_separates_undeclared_from_missing() -> None:
    """ "선언이 없다"와 "제1조가 없다"를 다른 목록으로 센다. 조치가 다르기 때문이다."""
    graph = build_delegation_graph(
        {
            "X-GUIDE-001": "이 지침은 「정책」(X-POL-001)에서 위임된 사항을 정한다.",
            "X-POL-001": "이 정책은 기본 방향을 정한다.",
            "X-PROC-001": None,
        }
    )

    assert [(e.child_doc_id, e.parent_doc_id) for e in graph.edges] == [
        ("X-GUIDE-001", "X-POL-001")
    ]
    assert graph.undeclared == ("X-POL-001",)
    assert graph.missing_article == ("X-PROC-001",)


def test_dangling_parent_is_recorded_not_raised() -> None:
    """상위 문서를 갖고 있지 않은 간선은 오류가 아니라 기록이다 — 적재 범위의 사실이다."""
    graph = build_delegation_graph(
        {"X-GUIDE-001": "이 지침은 「정책」(X-POL-001)에서 위임된 사항을 정한다."}
    )

    assert graph.edges == ()
    assert [e.parent_doc_id for e in graph.dangling] == ["X-POL-001"]


def test_cycle_is_rejected() -> None:
    """순환이면 그래프를 만들지 않는다. 승격 순서가 정해지지 않기 때문이다."""
    with pytest.raises(DelegationError, match="순환"):
        build_delegation_graph(
            {
                "X-A-001": "이 지침은 「비」(X-B-001)에서 위임된 사항을 정한다.",
                "X-B-001": "이 지침은 「에이」(X-A-001)에서 위임된 사항을 정한다.",
            }
        )


def test_self_reference_is_a_cycle() -> None:
    """자기 자신을 위임 대상으로 지목하는 것도 순환이다."""
    with pytest.raises(DelegationError):
        build_delegation_graph({"X-A-001": "이 지침은 「에이」(X-A-001)에서 위임된 사항을 정한다."})


def test_real_corpus_yields_four_edges() -> None:
    """**실제 코퍼스에서 간선 4개가 나온다.** 이 수가 줄면 승격이 조용히 약해진다.

    `ISP-POL-001` 만 선언이 없고(최상위이므로 정상), `ISP-PROC-002` 만 조를 지목한다.
    """
    first_articles: dict[str, str | None] = {}
    for path in sorted(CORPUS.glob("ISP-*.md")):
        document = parse_policy_document(path)
        article = document.by_article_no.get(DELEGATION_ARTICLE_NO)
        first_articles[document.doc_id] = article.text_raw if article else None

    graph = build_delegation_graph(first_articles)
    edges = {(e.child_doc_id, e.parent_doc_id, e.parent_article_no) for e in graph.edges}

    assert edges == {
        ("ISP-GUIDE-002", "ISP-POL-001", None),
        ("ISP-GUIDE-003", "ISP-POL-001", None),
        ("ISP-PROC-001", "ISP-GUIDE-002", None),
        ("ISP-PROC-002", "ISP-GUIDE-002", 35),
    }
    assert graph.undeclared == ("ISP-POL-001",)
    assert graph.missing_article == ()
    assert graph.dangling == ()
