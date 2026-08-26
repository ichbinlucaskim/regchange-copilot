"""위임 승격이 **1차 검색을 고치지 않고 후보만 더하는지** 확인한다 (R-22, ADR-017).

이 테스트가 존재하는 이유: 승격이 1차 결과의 순위를 바꾸면 3단계 검색 측정과의 비교가
끊긴다. 그 비교가 끊기면 "승격이 재현율을 올렸다"는 주장을 확인할 방법이 없어진다.
그리고 승격 문단에 출처 표시가 붙지 않으면, 측정이 재현율 상승과 정밀도 하락을 분리할 수
없다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from regchange.guards.killswitch import SwitchGate
from regchange.retrieval.index import embed_corpus
from regchange.retrieval.models import PromotionMechanism, RetrievalSource, SearchMode
from regchange.retrieval.promote import load_delegation_graph, promote_by_delegation
from regchange.retrieval.search import search

pytestmark = pytest.mark.requires_db

AS_OF = dt.date(2026, 2, 1)
NOW = dt.datetime(2026, 8, 21, 9, 0, tzinfo=dt.UTC)

POLICY_FIRST = "이 정책은 은행의 정보보호 기본 방향을 정함을 목적으로 한다."
GUIDE_FIRST = "이 지침은 「정보보호정책」(ISP-POL-001)에서 위임된 사항과 세부 기준을 정한다."
PROC_FIRST = (
    "이 절차서는 「정보보호 관리지침」(ISP-GUIDE-002) 제35조에서 위임된 사항으로서 "
    "침해사고 대응 절차를 정한다."
)


class StubEmbedding:
    """결정론적 벡터. 이 테스트는 순위가 아니라 **승격 여부와 표시**를 본다."""

    model_id = "stub:embedding"
    dimensions = 4

    def embed_documents(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(t) for t in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        return (1.0, (sum(ord(c) for c in text) % 97) / 100.0, 0.0, 0.0)


async def _document(
    conn: psycopg.AsyncConnection[Any], doc_id: str, articles: dict[int, tuple[str, str]]
) -> dict[int, UUID]:
    """문서 하나와 그 조들을 넣고 `{조 번호: 문단 id}` 를 돌려준다."""
    document_id = uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO policy_document (
                id, doc_id, version, title, owner_dept, classification,
                effective_date, source_path, source_sha256, known_from
            ) VALUES (%s, %s, '1.0', %s, '정보보호부', 'INTERNAL',
                      DATE '2025-06-01', 'x.md', repeat('a', 64), %s)
            """,
            (document_id, doc_id, doc_id, NOW),
        )
        ids: dict[int, UUID] = {}
        for number, (title, text) in articles.items():
            paragraph_id = uuid4()
            ids[number] = paragraph_id
            await cur.execute(
                """
                INSERT INTO policy_paragraph (
                    id, document_id, article_no, article_title, seq_in_doc,
                    text_raw, text_norm, text_norm_sha256, norm_rule_version, known_from
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, repeat('b', 64), 'norm-v2', %s)
                """,
                (paragraph_id, document_id, number, title, number, text, text, NOW),
            )
    await conn.commit()
    return ids


async def _seed(conn: psycopg.AsyncConnection[Any]) -> dict[str, dict[int, UUID]]:
    """정책 → 지침 → 절차서 3계층. 절차서만 조를 지목한 위임을 갖는다."""
    policy = await _document(
        conn,
        "ISP-POL-001",
        {
            1: ("목적", POLICY_FIRST),
            5: ("경영진의 책임", "경영진은 정보보호 활동에 필요한 자원을 지원한다."),
            9: ("임직원의 의무", "임직원은 이 정책과 하위 규정을 준수하여야 한다."),
        },
    )
    guide = await _document(
        conn,
        "ISP-GUIDE-002",
        {
            1: ("목적", GUIDE_FIRST),
            35: ("침해사고 보고", "정보보호부장은 침해사고를 2시간 이내에 보고한다."),
        },
    )
    proc = await _document(
        conn,
        "ISP-PROC-002",
        {
            1: ("목적", PROC_FIRST),
            7: ("침해사고 신고", "정보보호부장은 침해사고를 인지한 즉시 신고한다."),
        },
    )
    await embed_corpus(conn, StubEmbedding())  # type: ignore[arg-type]
    await conn.commit()
    return {"ISP-POL-001": policy, "ISP-GUIDE-002": guide, "ISP-PROC-002": proc}


async def test_delegation_graph_is_read_from_document_bodies(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """관계를 **본문에서** 읽는다. 메타데이터를 따로 두지 않는다 (ADR-017)."""
    await _seed(owner_conn)

    graph = await load_delegation_graph(owner_conn, as_of=AS_OF)

    edges = {(e.child_doc_id, e.parent_doc_id, e.parent_article_no) for e in graph.edges}
    assert edges == {
        ("ISP-GUIDE-002", "ISP-POL-001", None),
        ("ISP-PROC-002", "ISP-GUIDE-002", 35),
    }
    assert graph.undeclared == ("ISP-POL-001",), "최상위 문서에 위임이 없는 것은 정상이다"


async def test_promotion_adds_candidates_without_touching_the_primary_result(
    owner_conn: psycopg.AsyncConnection[Any],
    switches_on: SwitchGate,
) -> None:
    """**1차 결과의 순서와 점수가 그대로다.** 승격은 추가이지 재순위가 아니다."""
    await _seed(owner_conn)
    embedding = StubEmbedding()
    base = await search(
        owner_conn,
        switches=switches_on,
        query="침해사고 신고 기한",
        mode=SearchMode.HYBRID,
        limit=3,
        as_of=AS_OF,
        client=embedding,  # type: ignore[arg-type]
    )

    promoted = await promote_by_delegation(
        owner_conn,
        switches=switches_on,
        result=base,
        query="침해사고 신고 기한",
        as_of=AS_OF,
        top_n=1,
        client=embedding,  # type: ignore[arg-type]
    )

    before = [(c.paragraph_id, c.score) for c in base.chunks]
    after = [(c.paragraph_id, c.score) for c in promoted.primary]
    assert after == before, "승격이 1차 결과를 바꿨다 — 기존 측정과 비교가 끊긴다"
    assert len(promoted.chunks) > len(base.chunks)


async def test_declared_article_is_promoted_without_research(
    owner_conn: psycopg.AsyncConnection[Any],
    switches_on: SwitchGate,
) -> None:
    """문서가 조를 지목했으면 재검색 없이 그 조를 올린다 — 문서가 우리 검색보다 정확하다."""
    ids = await _seed(owner_conn)
    embedding = StubEmbedding()
    # 지목된 조가 1차 결과에 없어야 승격이 관측된다. 절차서 문단만 잡히도록 좁게 검색한다.
    base = await search(
        owner_conn,
        switches=switches_on,
        query="침해사고 신고",
        mode=SearchMode.LEXICAL,
        limit=1,
        as_of=AS_OF,
        client=embedding,  # type: ignore[arg-type]
    )
    assert base.chunks[0].doc_id == "ISP-PROC-002"
    assert ids["ISP-GUIDE-002"][35] not in {c.paragraph_id for c in base.chunks}

    result = await promote_by_delegation(
        owner_conn,
        switches=switches_on,
        result=base,
        query="침해사고 신고",
        as_of=AS_OF,
        top_n=1,
        client=embedding,  # type: ignore[arg-type]
        mode=SearchMode.LEXICAL,
    )

    declared = [
        c
        for c in result.promoted
        if c.promotion is not None and c.promotion.mechanism is PromotionMechanism.DECLARED_ARTICLE
    ]
    assert declared, "지목된 조(ISP-GUIDE-002 제35조)가 올라오지 않았다"
    assert declared[0].paragraph_id == ids["ISP-GUIDE-002"][35]
    assert declared[0].score == 0.0, "재검색하지 않았으므로 점수가 없다 — 지어내지 않는다"
    assert declared[0].promotion is not None
    assert "위임" in declared[0].promotion.delegation_quote


async def test_promoted_chunks_carry_their_reason(
    owner_conn: psycopg.AsyncConnection[Any],
    switches_on: SwitchGate,
) -> None:
    """승격 문단이 **왜 올라왔는지**를 들고 다닌다. 없으면 검토 화면이 답할 수 없다."""
    await _seed(owner_conn)
    embedding = StubEmbedding()
    base = await search(
        owner_conn,
        switches=switches_on,
        query="정보보호 자원 지원",
        mode=SearchMode.HYBRID,
        limit=8,
        as_of=AS_OF,
        client=embedding,  # type: ignore[arg-type]
    )

    result = await promote_by_delegation(
        owner_conn,
        switches=switches_on,
        result=base,
        query="정보보호 자원 지원",
        as_of=AS_OF,
        top_n=2,
        client=embedding,  # type: ignore[arg-type]
    )

    for chunk in result.promoted:
        assert chunk.source is RetrievalSource.DELEGATION_PROMOTED
        assert chunk.promotion is not None
        assert chunk.promotion.via_doc_id
        assert chunk.promotion.delegation_quote
    assert result.delegation is not None
    assert result.delegation.top_n == 2
    assert result.delegation.promoted == len(result.promoted)


async def test_promotion_never_duplicates_a_primary_paragraph(
    owner_conn: psycopg.AsyncConnection[Any],
    switches_on: SwitchGate,
) -> None:
    """이미 1차에 있는 문단은 승격하지 않는다 — 중복은 인용 검증을 통과시키는 방향이다."""
    await _seed(owner_conn)
    embedding = StubEmbedding()
    base = await search(
        owner_conn,
        switches=switches_on,
        query="침해사고",
        mode=SearchMode.HYBRID,
        limit=20,  # 코퍼스 전체가 들어온다
        as_of=AS_OF,
        client=embedding,  # type: ignore[arg-type]
    )

    result = await promote_by_delegation(
        owner_conn,
        switches=switches_on,
        result=base,
        query="침해사고",
        as_of=AS_OF,
        top_n=3,
        client=embedding,  # type: ignore[arg-type]
    )

    ids = [c.paragraph_id for c in result.chunks]
    assert len(ids) == len(set(ids)), "승격이 중복 문단을 만들었다"
    assert result.promoted == ()


async def test_zero_primary_results_promote_nothing(
    owner_conn: psycopg.AsyncConnection[Any],
    switches_on: SwitchGate,
) -> None:
    """1차 결과가 0건이면 출발점이 없다. `promoted=0` 인 리포트는 남는다.

    "시도했으나 올릴 것이 없었다"와 "시도하지 않았다"는 다른 사실이다.
    """
    await _seed(owner_conn)
    embedding = StubEmbedding()
    empty = await search(
        owner_conn,
        switches=switches_on,
        query="침해사고",
        mode=SearchMode.HYBRID,
        limit=1,
        as_of=dt.date(2020, 1, 1),  # 코퍼스 시행일 이전
        client=embedding,  # type: ignore[arg-type]
    )
    assert empty.chunks == ()

    result = await promote_by_delegation(
        owner_conn,
        switches=switches_on,
        result=empty,
        query="침해사고",
        as_of=dt.date(2020, 1, 1),
        top_n=2,
        client=embedding,  # type: ignore[arg-type]
    )

    assert result.chunks == ()
    assert result.delegation is not None
    assert result.delegation.promoted == 0
