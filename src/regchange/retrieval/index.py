"""사내 규정 코퍼스 적재와 임베딩 색인 구축.

목적:
    파싱된 `PolicyDocument` 를 `policy_document` / `policy_paragraph` 에 적재하고,
    각 문단의 임베딩을 `policy_paragraph_embedding` 에 넣는다.

구현 이유:
    **문서 단위 트랜잭션을 쓴다.** `store/loader.py` 와 같은 판단이다 — 부분 상태가
    존재하되 그 부분이 의미 있는 단위로 끊긴다. 문서 하나의 조 하나가 실패하면 그
    문서 전체가 되돌아가고, 다른 문서는 이미 적재된 채로 남는다.

    **적재와 임베딩을 분리한다.** 임베딩은 모델마다 다시 만들어야 하고(비교 측정),
    실패 원인도 다르다(네트워크·모델 로딩 vs 스키마·제약). 한 함수로 묶으면 임베딩
    모델을 바꿀 때마다 문단을 다시 적재하려 들게 되고, 그것은 bitemporal 테이블에서
    유니크 충돌로 나타난다.

    **벡터를 문자열 리터럴로 보내고 `::vector` 로 캐스팅한다.** `pgvector` 파이썬
    어댑터 등록에 기대지 않는다. 등록을 잊은 커넥션 하나가 평소엔 잘 돌다가 특정
    경로에서만 터지는 실패 모드를 만들지 않기 위해서다 — `store/timestamps.py` 가
    전역 등록을 택한 것과 같은 문제의식이되, 여기서는 등록 자체가 필요 없게 했다.

트레이드오프:
    - 재적재 시 조용히 덮어쓰지 않고 실패한다. 편의를 포기한 대신 "같은 버전인데
      내용이 다른 문서"가 소리 없이 코퍼스를 바꾸는 일이 없다. 문서를 고쳤으면
      `version` 을 올리는 것이 규정 문서의 정상 절차다.
    - 임베딩은 `ON CONFLICT DO UPDATE` 로 덮어쓴다. 파생 캐시이므로 이력이 필요 없고,
      같은 모델로 다시 돌렸을 때 실패하면 측정 반복이 불가능해진다.
    - 벡터를 문자열로 직렬화하므로 요청 크기가 이진 전송보다 크다. 152개 문단, 3072 차원 기준에서
      수 MB 수준이며 로컬 DB 에서 무시할 수 있다. 수십만 문단 규모에서는 다시 재야 한다.

엣지 케이스:
    - 같은 `(doc_id, version)` 이 이미 있고 `source_sha256` 이 같음: 문서를 건너뛴다.
      멱등 재실행이며 정상 경로다.
    - 같은 `(doc_id, version)` 인데 `source_sha256` 이 다름: `CorpusLoadError`.
      파일이 바뀌었는데 버전을 올리지 않은 것이며, 조용히 넘기면 채점 결과가 어느
      문서에 대한 것인지 알 수 없게 된다.
    - 문단은 있는데 임베딩이 없음: `embed_corpus` 가 없는 것만 만든다. 중간에 죽은
      이전 실행을 이어서 끝낼 수 있다.
    - 임베딩 대상 문단이 0건: 경고 로그를 남기고 아무것도 하지 않는다. 예외로 올리지
        않는 이유는 "이미 전부 임베딩됨"이 정상 상태이기 때문이다.
    - 모델이 반환한 벡터 수가 문단 수와 다름: `CorpusLoadError`. 문단과 벡터가
      어긋난 채 적재되면 검색 결과가 조용히 무의미해진다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from regchange.adapters.embedding import EmbeddingClient
from regchange.retrieval.models import PolicyDocument

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 32
"""한 번에 임베딩할 문단 수. 어댑터가 다시 쪼갤 수 있으므로 상한이 아니라 진행 단위다.

작게 두는 이유: 중간에 실패해도 앞의 배치는 이미 커밋돼 있어 이어서 돌릴 수 있다."""


class CorpusLoadError(RuntimeError):
    """코퍼스 적재를 계속할 수 없다. 부분 상태를 조용히 남기지 않는다."""


def _vector_literal(vector: Sequence[float]) -> str:
    """벡터를 pgvector 리터럴 문자열로 만든다. `[0.1,0.2,…]` 형식이다."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


async def load_document(
    conn: psycopg.AsyncConnection[Any],
    document: PolicyDocument,
    *,
    known_from: dt.datetime,
) -> tuple[UUID, int]:
    """문서 하나와 그 조 전체를 적재하고 `(문서 id, 적재된 조 수)` 를 반환한다.

    목적:
        파싱 결과를 bitemporal 테이블에 넣는다. 이 문단 ID 들이 이후 인용 검증의
        정답 집합이 된다 (원칙 2).

    구현 이유:
        이미 있는 문서를 만나면 `source_sha256` 을 대조한 뒤 건너뛴다. `ON CONFLICT
        DO NOTHING` 이 더 짧지만, 그러면 "이미 있어서 건너뛴 것"과 "내용이 달라
        충돌한 것"이 같은 무반응으로 뭉개진다. 그 구별이 코퍼스 관리의 요구사항이다.

    트레이드오프:
        문서마다 SELECT 를 한 번 더 한다. 5개 문서 규모에서 무의미한 비용이며,
        얻는 것은 위의 구별이다.

    엣지 케이스:
        모듈 docstring 참조. 조 수가 0인 문서는 파서가 이미 거부한다.
    """
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT id, source_sha256
              FROM policy_document
             WHERE doc_id = %(doc_id)s
               AND version = %(version)s
               AND known_until = 'infinity'
            """,
            {"doc_id": document.doc_id, "version": document.version},
        )
        existing = await cursor.fetchone()

    if existing is not None:
        if existing["source_sha256"] != document.source_sha256:
            msg = (
                f"{document.label}: 같은 버전인데 파일 내용이 다르다 "
                f"(DB={existing['source_sha256'][:12]}… "
                f"파일={document.source_sha256[:12]}…). "
                "문서를 고쳤으면 version 을 올린다"
            )
            raise CorpusLoadError(msg)
        logger.info("이미 적재됨, 건너뜀: %s", document.label)
        return UUID(str(existing["id"])), 0

    document_id = uuid4()
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            INSERT INTO policy_document (
                id, doc_id, version, title, owner_dept, classification,
                effective_date, parent_laws, revision_history,
                source_path, source_sha256, known_from
            ) VALUES (
                %(id)s, %(doc_id)s, %(version)s, %(title)s, %(owner_dept)s,
                %(classification)s, %(effective_date)s, %(parent_laws)s,
                %(revision_history)s, %(source_path)s, %(source_sha256)s, %(known_from)s
            )
            """,
            {
                "id": document_id,
                "doc_id": document.doc_id,
                "version": document.version,
                "title": document.title,
                "owner_dept": document.owner_dept,
                "classification": document.classification,
                "effective_date": document.effective_date,
                "parent_laws": Jsonb(list(document.parent_laws)),
                "revision_history": Jsonb([dict(entry) for entry in document.revision_history]),
                "source_path": document.source_path,
                "source_sha256": document.source_sha256,
                "known_from": known_from,
            },
        )

        for article in document.articles:
            await cursor.execute(
                """
                INSERT INTO policy_paragraph (
                    id, document_id, article_no, article_title, seq_in_doc,
                    text_raw, text_norm, text_norm_sha256, norm_rule_version, known_from
                ) VALUES (
                    %(id)s, %(document_id)s, %(article_no)s, %(article_title)s,
                    %(seq_in_doc)s, %(text_raw)s, %(text_norm)s, %(text_norm_sha256)s,
                    %(norm_rule_version)s, %(known_from)s
                )
                """,
                {
                    "id": uuid4(),
                    "document_id": document_id,
                    "article_no": article.article_no,
                    "article_title": article.article_title,
                    "seq_in_doc": article.seq_in_doc,
                    "text_raw": article.text_raw,
                    "text_norm": article.text_norm,
                    "text_norm_sha256": article.text_norm_sha256,
                    "norm_rule_version": article.norm_rule_version,
                    "known_from": known_from,
                },
            )

    logger.info("적재: %s, 조 %d건", document.label, len(document.articles))
    return document_id, len(document.articles)


async def load_corpus_documents(
    conn: psycopg.AsyncConnection[Any],
    documents: Sequence[PolicyDocument],
    *,
    known_from: dt.datetime | None = None,
) -> dict[str, int]:
    """문서 여러 건을 문서 단위 트랜잭션으로 적재한다.

    목적:
        코퍼스 전체를 한 번에 넣되, 한 문서의 실패가 다른 문서를 되돌리지 않게 한다.

    구현 이유:
        `known_from` 을 인자로 받아 호출 전체가 같은 값을 쓰게 한다. 문서마다
        `now()` 를 부르면 한 번의 적재가 여러 인지 시각을 갖게 되고, "이 코퍼스는
        언제부터 알려졌는가"에 하나의 답이 나오지 않는다.

    트레이드오프:
        실패한 문서 이후의 문서를 계속 적재하지 않고 예외를 전파한다. 부분 코퍼스로
        측정을 돌리면 지표가 코퍼스 구성의 함수가 되므로, 여기서는 멈추는 것이 맞다.

    엣지 케이스:
        - 빈 목록: 빈 dict. 호출부가 파서 단계에서 이미 막는다.
    """
    stamp = known_from or dt.datetime.now(dt.UTC)
    loaded: dict[str, int] = {}
    for document in documents:
        async with conn.transaction():
            _, count = await load_document(conn, document, known_from=stamp)
        loaded[document.label] = count
    return loaded


async def embed_corpus(
    conn: psycopg.AsyncConnection[Any],
    client: EmbeddingClient,
    *,
    as_of: dt.date | None = None,
) -> int:
    """임베딩이 없는 문단에 대해 임베딩을 만들어 적재하고 건수를 반환한다.

    목적:
        검색이 쓸 벡터 색인을 만든다. 모델별로 독립적이며 여러 번 돌려도 안전하다.

    구현 이유:
        **`text_norm` 을 임베딩한다.** 인용은 `text_raw` 를 가리키지만 검색 인덱스의
        입력은 정규화본이다 (ADR-002). 공백·줄바꿈 변형이 벡터를 흔들지 않아야
        같은 내용의 문단이 같은 자리에 놓인다.

        **이미 있는 것을 다시 만들지 않는다.** 측정을 반복하면서 매번 152건을 다시
        임베딩하면 API 비용과 시간이 선형으로 늘고, 그 비용은 아무 정보도 주지 않는다.

    트레이드오프:
        배치 단위로 커밋한다. 전체를 한 트랜잭션으로 묶으면 중간 실패 시 처음부터
        다시 해야 하고, 문단마다 커밋하면 왕복이 152번이다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT p.id, p.text_norm
              FROM policy_paragraph p
              JOIN policy_document d ON d.id = p.document_id
             WHERE p.known_until = 'infinity'
               AND d.known_until = 'infinity'
               AND (%(as_of)s::date IS NULL OR d.effective_date <= %(as_of)s::date)
               AND NOT EXISTS (
                     SELECT 1
                       FROM policy_paragraph_embedding e
                      WHERE e.paragraph_id = p.id
                        AND e.model_id = %(model_id)s
                   )
             ORDER BY p.id
            """,
            {"as_of": as_of, "model_id": client.model_id},
        )
        pending = await cursor.fetchall()

    if not pending:
        logger.warning("임베딩할 문단이 없다: %s", client.model_id)
        return 0

    dimensions = client.dimensions
    done = 0
    for start in range(0, len(pending), EMBED_BATCH_SIZE):
        batch = pending[start : start + EMBED_BATCH_SIZE]
        vectors = client.embed_documents([str(row["text_norm"]) for row in batch])
        if len(vectors) != len(batch):
            msg = f"벡터 수가 문단 수와 다르다: 문단 {len(batch)} 벡터 {len(vectors)}"
            raise CorpusLoadError(msg)

        async with conn.transaction(), conn.cursor() as cursor:
            for row, vector in zip(batch, vectors, strict=True):
                await cursor.execute(
                    """
                    INSERT INTO policy_paragraph_embedding
                        (paragraph_id, model_id, dim, embedding)
                    VALUES (%(paragraph_id)s, %(model_id)s, %(dim)s, %(embedding)s::vector)
                    ON CONFLICT (paragraph_id, model_id)
                    DO UPDATE SET dim = EXCLUDED.dim,
                                  embedding = EXCLUDED.embedding,
                                  embedded_at = now()
                    """,
                    {
                        "paragraph_id": row["id"],
                        "model_id": client.model_id,
                        "dim": dimensions,
                        "embedding": _vector_literal(vector),
                    },
                )
        done += len(batch)
        logger.info("임베딩 진행: %s %d/%d", client.model_id, done, len(pending))
    return done
