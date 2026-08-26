"""인젝션 스캔 **범위**를 바꾼 뒤 감도를 다시 잰다 (R-23 ③).

    uv run python -m evals.runners.injection_scan

무엇을 재는가:

1. **외부 텍스트(개정 조문)에서 몇 건이 발화하는가** — 골든셋 15건의 before/after 전문.
2. **사내 문단에서 몇 건이 발화했을 것인가** — 이제 스캔하지 않는 텍스트다. 이 수가
   범위 수정으로 사라진 오탐이며, R-23 이 관측한 case-012 가 여기 들어 있어야 한다.
3. **기록된 실측과 대조** — `llm_invocation.injection_signals_json` 에 남은 과거 실행의
   신호. "이전"은 추정이 아니라 **행**이다.

**감도를 바꾸지 않았다.** 패턴 목록도 정규식도 4단계와 같다. 그래야 이 측정이 범위
변경의 효과만 재게 된다 (R-23 의 조치 순서 ①②③).

**이 러너는 사내 텍스트에 패턴을 직접 돌린다.** 운영 경로에서는 타입(`UntrustedText`)이
그것을 막지만, 여기서는 "막지 않았다면 무엇이 발화했을까"를 세는 것이 목적이므로 공개
상수인 `INJECTION_PATTERNS` 를 직접 컴파일한다. **운영 코드가 이 방식을 따라 하면
R-23 이 되돌아온다** — 이 파일은 측정 코드이고 `evals/` 밖으로 나가지 않는다.

측정만 하며 모델을 부르지 않는다. 비용 0, 결정론적이다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import yaml

from regchange.config.settings import apply_dotenv
from regchange.guards.injection import INJECTION_PATTERNS
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

PATTERNS = tuple(
    (name, re.compile(pattern, re.IGNORECASE | re.MULTILINE))
    for name, pattern in INJECTION_PATTERNS
)
"""운영과 **같은 목록, 같은 플래그**. 감도를 바꾸지 않았다는 것의 실체다."""

POLICY_QUERY = """
SELECT d.doc_id, p.article_no, p.article_title, p.text_raw
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
 WHERE p.known_until = 'infinity' AND d.known_until = 'infinity'
 ORDER BY d.doc_id, p.seq_in_doc
"""

RECORDED_QUERY = """
SELECT purpose, injection_signals_json
  FROM llm_invocation
 WHERE jsonb_array_length(injection_signals_json) > 0
"""


def scan_text(text: str) -> tuple[str, ...]:
    """측정용 스캔. 운영의 `guards.injection.scan` 과 같은 판정을 하되 타입을 요구하지 않는다."""
    return tuple(sorted({name for name, pattern in PATTERNS if pattern.search(text)}))


def scan_amendments() -> list[dict[str, Any]]:
    """골든셋 개정 조문 — **범위 수정 후에도 스캔되는 텍스트**."""
    rows: list[dict[str, Any]] = []
    for path in sorted(GOLDEN_DIR.glob("case-*.yaml")):
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
        source = case["source"]
        text = "\n".join(filter(None, [source.get("before"), source.get("after")]))
        rows.append(
            {
                "case_id": case["id"],
                "article_path": source["article_path"],
                "signals": list(scan_text(text)),
                "chars": len(text),
            }
        )
    return rows


async def scan_policy(conn: psycopg.AsyncConnection[Any]) -> list[dict[str, Any]]:
    """사내 규정 문단 — **범위 수정으로 스캔 대상에서 빠진 텍스트**."""
    cur = await conn.execute(POLICY_QUERY)
    rows = []
    for doc_id, article_no, article_title, text_raw in await cur.fetchall():
        signals = scan_text(text_raw)
        if signals:
            rows.append(
                {
                    "doc_id": doc_id,
                    "spec": f"제{article_no}조 ({article_title})",
                    "signals": list(signals),
                    "excerpt": text_raw[:120],
                }
            )
    return rows


async def recorded_signals(conn: psycopg.AsyncConnection[Any]) -> dict[str, int]:
    """과거 실행에 실제로 기록된 신호. 「이전」의 근거는 추정이 아니라 이 행들이다."""
    cur = await conn.execute(RECORDED_QUERY)
    counter: Counter[str] = Counter()
    for purpose, signals in await cur.fetchall():
        for signal in signals:
            counter[f"{purpose}:{signal}"] += 1
    return dict(sorted(counter.items()))


async def main() -> None:
    parser = argparse.ArgumentParser(description="인젝션 스캔 범위 재측정 (R-23 ③)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    apply_dotenv()
    total_paragraphs = 0
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        policy_hits = await scan_policy(conn)
        recorded = await recorded_signals(conn)
        cur = await conn.execute(
            "SELECT count(*) FROM policy_paragraph WHERE known_until = 'infinity'"
        )
        row = await cur.fetchone()
        total_paragraphs = int(row[0]) if row else 0

    amendments = scan_amendments()
    amendment_hits = [row for row in amendments if row["signals"]]

    summary = {
        "measured_at": dt.datetime.now(dt.UTC).isoformat(),
        "scope": "amended_article only (R-23 ②)",
        "patterns": [name for name, _ in INJECTION_PATTERNS],
        "amendments": {
            "cases": len(amendments),
            "cases_with_signal": len(amendment_hits),
            "detail": amendment_hits,
        },
        "policy_paragraphs": {
            "total": total_paragraphs,
            "would_have_fired": len(policy_hits),
            "detail": policy_hits,
        },
        "recorded_before": recorded,
    }

    out = args.out or RESULTS_DIR / f"injection-scope-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "",
        "인젝션 스캔 범위 재측정 (R-23 ③) — 감도 무변경, 범위만 수정",
        f"  스캔 대상(개정 조문)     : {len(amendments)}건 중 발화 {len(amendment_hits)}건",
        f"  제외됨(사내 문단)        : {total_paragraphs}개 중 발화했을 것 {len(policy_hits)}건",
        "",
        "  사내 문단에서 사라진 발화:",
    ]
    lines.extend(
        f"    - {hit['doc_id']} {hit['spec']}: {', '.join(hit['signals'])}" for hit in policy_hits
    )
    lines.extend(["", "  과거 기록(llm_invocation):"])
    lines.extend(f"    - {key}: {count}" for key, count in recorded.items())
    lines.extend(["", f"  결과: {out.relative_to(REPO_ROOT)}", ""])
    print("\n".join(lines))  # noqa: T201 — 이 러너의 결과물은 사람이 읽는 표준출력이다


if __name__ == "__main__":
    asyncio.run(main())
