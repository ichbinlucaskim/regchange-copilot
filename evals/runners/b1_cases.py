"""골든셋 케이스별 B-1(조 번호 문자열 매칭) 실측 — **케이스 파일의 `b1_*` 필드의 출처**.

    uv run python -m evals.runners.b1_cases

목적:
    케이스 하나하나에 대해 "조 번호 문자열 매칭만으로 사내 규정 어디가 걸리는가"를
    센다. 그 결과가 각 케이스 YAML 의 `b1_matched` / `b1_matched_articles` 다.

구현 이유:
    **손으로 세지 않기 위해서다.** 이 저장소에서 손으로 센 것이 세 번 틀렸다
    (부처 코드 172 → 62, 이동 표기 12 → 15, 골든셋 총계 43 → 42). `b1_matched` 는
    42건에 사내 152조를 훑어야 나오는 값이라 손으로 셀 수 있는 종류가 아니다.

    **`evals.runners.baseline.article_tokens` 를 그대로 재사용한다.** B-1 은 이미
    `docs/16-baseline-comparison.md` 가 정의한 베이스라인이고, 여기서 토크나이저를
    다시 쓰면 케이스 파일의 값과 베이스라인 측정치가 **같은 B-1 을 뜻하지 않게 된다.**

    `b1_precheck.py` 와 다른 점: 저쪽은 **스냅샷의 변경 조문 전부**를 훑어 케이스를
    고르기 전에 난이도를 본다. 이쪽은 **이미 고른 케이스의 `article_path`** 를 훑는다.
    그래서 이쪽은 스냅샷이 필요 없고, 스냅샷이 없는 원천(280277)도 잴 수 있다.

트레이드오프:
    조 번호만 보고 법령명을 보지 않는다(`article_tokens` 의 판단을 그대로 따른다).
    사내 규정이 **다른 법의 같은 조 번호**를 인용했어도 걸린다. 그 방향은 베이스라인을
    강하게 만들어 우리에게 불리하므로 허용한다 — 그리고 그 오탐의 성격을 케이스가
    `b1_match_kind` 로 기록한다. **어느 성격으로 걸렸는지는 이 스크립트가 정하지
    못한다** — 사내 문서를 읽어야 알 수 있는 판단이라 사람이 YAML 에 적는다.

엣지 케이스:
    - `article_path` 에서 조 번호를 못 읽음: 토큰 0개. **"훑었는데 없음"이 아니라
      "훑을 것이 없음"이므로** `probed=False` 로 구별해 낸다. 둘을 같은 0 으로 세면
      B-1 이 실제보다 약해 보인다.
    - 사내 코퍼스가 비어 있음: `RuntimeError`. 0건을 정상 결과로 돌려주면 모든 케이스가
      "B-1 안 걸림"이 되고, 그 결과는 **틀렸다는 신호를 하나도 내지 않는다.**
    - 같은 조가 여러 토큰에 걸림: 표기(`ISP-PROC-002#7`)로 중복 제거한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import psycopg
import yaml

from evals.runners.baseline import PARAGRAPH_QUERY, article_tokens
from regchange.config.settings import apply_dotenv
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

AS_OF = "2026-02-01"
"""검색 규약과 같은 시점 (`docs/10-retrieval-evaluation-protocol.md`). 다른 시점을 쓰면
B-1 이 본 문서와 러너가 본 문서가 달라진다."""

logger = logging.getLogger("b1-cases")


def load_cases() -> list[dict[str, Any]]:
    """골든셋 케이스를 전부 읽는다. `Any` 는 YAML 경계라 좁힐 수 없다."""
    out: list[dict[str, Any]] = []
    for path in sorted(GOLDEN_DIR.glob("case-*.yaml")):
        parsed: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        out.append(parsed)
    return out


def probe(case: dict[str, Any], corpus: list[tuple[str, int, str]]) -> dict[str, Any]:
    """케이스 하나의 B-1 결과를 만든다.

    `probed` 가 있는 이유는 빈 칸의 의미를 가르기 위해서다 — 토큰이 0개면 훑을 것이
    없었던 것이고, 토큰이 있는데 걸린 것이 0개면 훑었는데 없었던 것이다.
    """
    tokens = article_tokens(str(case["source"].get("article_path") or ""))
    if not tokens:
        return {
            "id": str(case["id"]),
            "mst": str(case["source"]["mst"]),
            "tokens": [],
            "probed": False,
            "matched": None,
            "matched_articles": [],
        }

    hits = [f"{doc}#{no}" for doc, no, text in corpus if any(token in text for token in tokens)]
    unique = list(dict.fromkeys(hits))
    return {
        "id": str(case["id"]),
        "mst": str(case["source"]["mst"]),
        "tokens": tokens,
        "probed": True,
        "matched": bool(unique),
        "matched_articles": unique,
    }


def write_report(out_path: Path, rows: list[dict[str, Any]]) -> None:
    """결과를 파일로 남긴다.

    **비동기 함수 밖에서 쓴다** — 이벤트 루프 안에서 동기 파일 I/O 를 하면 루프가
    멈추고, 린터(ASYNC240)가 그것을 에러로 잡는다.
    """
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("결과: %s", out_path)


async def run() -> list[dict[str, Any]]:
    apply_dotenv()
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        cur = await conn.execute(PARAGRAPH_QUERY, {"as_of": AS_OF})
        corpus = [(str(d), int(n), str(t)) for d, n, t in await cur.fetchall()]

    if not corpus:
        msg = (
            f"사내 규정 코퍼스가 비어 있다 (as_of={AS_OF}). 이 상태로 재면 42건 전부 "
            "「B-1 안 걸림」이 되고 그 결과는 틀렸다는 신호를 내지 않는다"
        )
        raise RuntimeError(msg)

    rows = [probe(case, corpus) for case in load_cases()]
    probed = [r for r in rows if r["probed"]]
    matched = [r for r in probed if r["matched"]]

    logger.info("사내 문단 %d개 · 케이스 %d건", len(corpus), len(rows))
    logger.info(
        "훑음 %d / 훑지 못함 %d (조 번호를 못 읽음) · B-1 걸림 %d",
        len(probed),
        len(rows) - len(probed),
        len(matched),
    )
    for row in rows:
        state = "훑지못함" if not row["probed"] else ("걸림" if row["matched"] else "없음")
        logger.info(
            "  %-9s %-7s %-5s %-28s %s",
            row["id"],
            row["mst"],
            state,
            ",".join(row["tokens"]) or "—",
            ",".join(row["matched_articles"]) or "—",
        )

    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="골든셋 케이스별 B-1 실측")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "b1-cases.json")
    args = parser.parse_args()
    rows = asyncio.run(run())
    if args.out is not None:
        write_report(args.out, rows)


if __name__ == "__main__":
    main()
