"""사내 규정 코퍼스를 적재하고 임베딩 색인을 만든다.

`docs/10-retrieval-evaluation-protocol.md` §1 의 1단계(임베딩 확정)를 위한 준비 단계다.
적재는 한 번, 임베딩은 모델마다 한 번씩 돌린다.

    uv run python -m evals.runners.policy_index --embed bge-m3
    uv run python -m evals.runners.policy_index --embed kure-v1

`--embed` 를 생략하면 적재만 한다. 임베딩 모델을 바꿔도 문단을 다시 적재하지 않는다 —
적재와 임베딩을 분리한 이유는 `retrieval/index.py` docstring 참조.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from pathlib import Path

import psycopg

from regchange.adapters.embedding import EmbeddingClient
from regchange.config.settings import apply_dotenv
from regchange.retrieval.corpus import load_corpus
from regchange.retrieval.index import embed_corpus, load_corpus_documents
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "evals" / "corpus" / "internal-policies"

logger = logging.getLogger("policy_index")


LOCAL_MODELS = {
    "bge-m3": "BAAI/bge-m3",
    "kure-v1": "nlpai-lab/KURE-v1",
}
"""대조하는 로컬 임베딩 2종. 선택 근거는 ADR-015.

둘 다 1024차원 · 8192 시퀀스 · 567,754,752 파라미터 · MIT 다. KURE-v1 이 bge-m3 를
한국어로 파인튜닝한 모델이기 때문이며(모델 카드), 그래서 이 대조는 **한국어 특화 학습
하나만 변수로 남긴다.** 차원이 같으므로 재인덱싱 외에 바꿀 것이 없다."""


def build_client(name: str) -> EmbeddingClient:
    """이름으로 임베딩 구현을 고른다. 조립은 여기서만 한다 (ADR-010).

    도메인 코드는 `EmbeddingClient` Protocol 만 보며, 어느 구현인지 모른다.
    import-linter 계약이 그 경계를 CI 에서 강제한다.
    """
    if name in LOCAL_MODELS:
        from regchange.adapters.embedding.local import LocalEmbeddingClient

        return LocalEmbeddingClient(LOCAL_MODELS[name])
    if name == "openai":
        from regchange.adapters.embedding.openai import OpenAIEmbeddingClient

        return OpenAIEmbeddingClient()
    msg = f"알 수 없는 임베딩 이름: {name}"
    raise SystemExit(msg)


async def run(embed: str | None) -> None:
    documents = load_corpus(CORPUS_DIR)
    total_articles = sum(len(document.articles) for document in documents)
    logger.info("코퍼스 파싱: 문서 %d종, 조 %d건", len(documents), total_articles)

    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.POLICY)) as conn:
        loaded = await load_corpus_documents(conn, documents, known_from=dt.datetime.now(dt.UTC))
        for label, count in loaded.items():
            logger.info("적재 %s: 조 %d건", label, count)

        if embed:
            client = build_client(embed)
            started = dt.datetime.now(dt.UTC)
            done = await embed_corpus(conn, client)
            elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
            logger.info(
                "임베딩 완료: model=%s 건수=%d 차원=%d 소요=%.1f초",
                client.model_id,
                done,
                client.dimensions,
                elapsed,
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # `.env` 를 프로세스 환경에 채운다 — DSN 과 임베딩 API 키가 거기 있다.
    # 이미 주입된 환경변수는 덮지 않는다 (`apply_dotenv` 의 setdefault).
    apply_dotenv()
    parser = argparse.ArgumentParser(description="사내 규정 코퍼스 적재와 임베딩")
    parser.add_argument("--embed", choices=[*LOCAL_MODELS, "openai"], default=None)
    args = parser.parse_args()
    asyncio.run(run(args.embed))


if __name__ == "__main__":
    main()
