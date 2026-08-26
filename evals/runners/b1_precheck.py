"""B-1 사전 확인 — **케이스를 고르기 전에 난이도를 안다** (6단계 §4).

    uv run python -m evals.runners.b1_precheck

무엇을 하는가:
    확보한 개정 조문 각각에 대해 **조 번호 문자열 매칭**(B-1)을 돌려, 그 조문이
    사내 규정 152조 중 어디에 걸리는지 본다. **LLM 을 부르지 않는다. 비용 0.**

왜 먼저 하는가:
    `docs/16-baseline-comparison.md` 가 확인했다 — **EASY 5건은 조 번호 문자열
    매칭으로 전부 잡힌다.** EASY 는 코퍼스 설계의 성질이지 우리 성과가 아니다.
    새 케이스가 B-1 에 걸리면 그것도 EASY 이고, 이미 5건 있다.

    그래서 **케이스를 고르기 전에** 어느 조문이 B-1 에 걸리는지 알아야 한다.
    순서를 뒤집으면 EASY 를 더 만들고 지표만 부풀린다.

**타법개정을 반드시 함께 본다.** 타법개정은 인용 조문 번호만 바뀌는 것이 많아
**문자열 매칭에 잘 걸리는데 정답은 「영향 없음」이다.** 그 조합이 지금 골든셋에
없으며, 정밀도를 시험하는 재료다.

빈 칸의 의미 (구조 감사 §4 의 예측을 따른다):
    이 러너의 결과에서 「걸린 사내 조항 0건」은 **한 가지 뜻뿐이다** — 매칭을
    돌렸고 걸린 것이 없다. 「매칭을 못 돌렸다」는 별도 값(`parse_failed`)으로
    남기며, 둘을 같은 0 으로 세지 않는다.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from regchange.config.settings import apply_dotenv
from regchange.parse.law_xml import ParseError, parse_law_document
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = REPO_ROOT / "data" / "snapshots"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
AS_OF = "2026-02-01"

PARAGRAPH_QUERY = """
SELECT d.doc_id, p.article_no, p.article_title, p.text_raw
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
 WHERE p.known_until = 'infinity' AND d.known_until = 'infinity'
   AND d.effective_date <= %(as_of)s
 ORDER BY d.doc_id, p.seq_in_doc
"""

logger = logging.getLogger("b1")


@dataclass(frozen=True, slots=True)
class ArticleProbe:
    """개정 조문 하나에 대한 B-1 결과."""

    mst: str
    law_name: str
    revision_kind: str
    article_key: str
    token: str
    title: str | None
    text_length: int
    matched: tuple[str, ...]
    """걸린 사내 조항 표기(`ISP-PROC-002#7`). 빈 튜플은 **돌렸는데 없음**이다."""


def find_snapshot(mst: str) -> Path | None:
    """해당 MST 의 본문 스냅샷 페이지를 찾는다. 여러 실행에 걸쳐 있을 수 있다."""
    candidates = sorted(SNAPSHOT_ROOT.glob(f"*/law/MST-{mst}-*/page-001.xml"))
    return candidates[-1] if candidates else None


def token_of(article_no: int, branch_no: int) -> str:
    """조 번호를 사내 규정이 인용했을 법한 표기로 만든다 (`제48조의3`)."""
    return f"제{article_no}조" + (f"의{branch_no}" if branch_no else "")


def probe_event(
    event: dict[str, Any], corpus: list[tuple[str, int, str]]
) -> tuple[list[ArticleProbe], str | None]:
    """이벤트 하나의 **변경된 조문**을 전부 훑는다.

    반환값이 `(결과, 실패사유)` 인 이유: 「걸린 것이 없다」와 「파싱을 못 했다」를
    같은 빈 목록으로 표현하지 않기 위해서다 (구조 감사 §5).
    """
    mst = str(event["mst"])
    page = find_snapshot(mst)
    if page is None:
        return [], "스냅샷 없음"
    try:
        document = parse_law_document(page)
    except ParseError as exc:
        return [], f"파싱 실패: {exc}"

    # **문자열 키로 대조하지 않는다.** 캐시(`lsJoHstInf`)의 `조문번호` 는 6자리
    # (조 4 + 가지 2)이고 파서의 `article_key` 는 7자리다. 문자열로 맞추면 조용히
    # 0건이 나오고, 그것은 "변경된 조문이 없다"와 구별되지 않는다.
    changed = {(int(str(k).zfill(6)[:4]), int(str(k).zfill(6)[4:6])) for k in event["articles"]}
    probes: list[ArticleProbe] = []
    for unit in document.articles:
        if (unit.article_no, unit.branch_no) not in changed:
            continue
        token = token_of(unit.article_no, unit.branch_no)
        body = unit.content.raw
        matched = tuple(f"{doc}#{no}" for doc, no, text in corpus if token in text)
        probes.append(
            ArticleProbe(
                mst=mst,
                law_name=str(event["law_name"]),
                revision_kind=str(event["revision_kind"]),
                article_key=unit.article_key,
                token=token,
                title=unit.title,
                text_length=len(body),
                matched=matched,
            )
        )
    return probes, None


def load_events(sources_file: Path) -> list[dict[str, Any]]:
    """원천 목록을 읽는다.

    **비동기 함수 밖에서 읽는다** — 이벤트 루프 안에서 동기 파일 I/O 를 하면 루프가
    멈추고, 린터(ASYNC240)가 그것을 에러로 잡는다.
    """
    loaded: list[dict[str, Any]] = json.loads(sources_file.read_text(encoding="utf-8"))["events"]
    return loaded


async def run(sources_file: Path, events: list[dict[str, Any]]) -> None:
    apply_dotenv()

    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        cur = await conn.execute(PARAGRAPH_QUERY, {"as_of": AS_OF})
        corpus = [(str(d), int(n), str(t)) for d, n, _, t in await cur.fetchall()]

    all_probes: list[ArticleProbe] = []
    failures: list[dict[str, str]] = []
    for event in events:
        probes, failure = probe_event(event, corpus)
        if failure is not None:
            failures.append({"mst": str(event["mst"]), "reason": failure})
            logger.warning("MST=%s 훑지 못함: %s", event["mst"], failure)
            continue
        # **훑었는데 0개**와 **못 훑었다**를 가른다. 캐시가 변경으로 표시한 조문이
        # 본문에 없으면 그것 자체가 관측이며 조용히 넘기지 않는다.
        if not probes:
            failures.append(
                {"mst": str(event["mst"]), "reason": "변경 조문이 본문에서 매칭되지 않음"}
            )
            logger.warning(
                "MST=%s 변경 조문 %d개가 본문에 없다", event["mst"], len(event["articles"])
            )
        all_probes.extend(probes)

    by_kind: Counter[str] = Counter()
    matched_by_kind: Counter[str] = Counter()
    for p in all_probes:
        by_kind[p.revision_kind] += 1
        if p.matched:
            matched_by_kind[p.revision_kind] += 1

    amended = [p for p in all_probes if p.revision_kind == "타법개정"]
    amended_matched = [p for p in amended if p.matched]

    report = {
        "sources_file": sources_file.name,
        "corpus_paragraphs": len(corpus),
        "articles_probed": len(all_probes),
        "parse_failures": failures,
        "by_kind": {
            kind: {"articles": n, "b1_matched": matched_by_kind[kind]}
            for kind, n in sorted(by_kind.items())
        },
        "amended_by_other_law": {
            "articles": len(amended),
            "b1_matched": len(amended_matched),
            "detail": [
                {
                    "mst": p.mst,
                    "token": p.token,
                    "title": p.title,
                    "matched": list(p.matched),
                }
                for p in amended_matched
            ],
        },
        "probes": [
            {
                "mst": p.mst,
                "law_name": p.law_name,
                "revision_kind": p.revision_kind,
                "article_key": p.article_key,
                "token": p.token,
                "title": p.title,
                "text_length": p.text_length,
                "b1_matched": bool(p.matched),
                "matched": list(p.matched),
            }
            for p in all_probes
        ],
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"b1-precheck-{sources_file.stem.split('-')[-1]}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("훑은 조문 %d개 (파싱 실패 %d건)", len(all_probes), len(failures))
    for kind, row in report["by_kind"].items():  # type: ignore[attr-defined]
        logger.info("  %-8s 조문 %3d개 중 B-1 걸림 %3d개", kind, row["articles"], row["b1_matched"])
    logger.info(
        "**타법개정 %d조문 중 B-1 에 걸리는 것 %d개** — 새 유형의 재료",
        len(amended),
        len(amended_matched),
    )
    logger.info("결과: %s", out)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="B-1 사전 확인 (6단계 §4)")
    parser.add_argument("--sources", type=Path, default=None)
    args = parser.parse_args()
    sources = args.sources or sorted(RESULTS_DIR.glob("expansion-sources-*.json"))[-1]
    import asyncio

    asyncio.run(run(sources, load_events(sources)))


if __name__ == "__main__":
    main()
