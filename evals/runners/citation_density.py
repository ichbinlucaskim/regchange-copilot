"""인용 밀도 대조 — **개정 조문 쪽에서 같은 질문을 잰다** (`docs/21-golden-42-results.md` §7).

    uv run python -m evals.runners.citation_density

무엇을 재는가:
    `docs/21` §7 의 남은 질문은 「기존 15건과 신규 27건 중 어느 쪽이 실제 운영 분포에
    가까운가」다. 그 질문의 대리 지표로 **사내 규정의 법령 인용 밀도 0.1447** 을 썼는데,
    그 값은 우리가 만든 합성 코퍼스의 성질이라 실무의 관측이 아니다(`docs/09` §5.3 —
    40% 를 14% 로 낮춘 것도 우리 결정이다).

    **법제처 개정 조문이 다른 법령을 얼마나 인용하는지는 실제 데이터다.** 우리가 만든
    것이 아니다. 그래서 같은 척도를 개정 조문 쪽에 대고, 두 밀도를 나란히 놓는다.

    **LLM 을 부르지 않는다. 비용 0.**

──────────────────────────────────────────────────────────────────────────────
인용의 정의 — **측정 전에 확정했고 결과를 보고 바꾸지 않는다**
──────────────────────────────────────────────────────────────────────────────

1. **단위는 조 하나다.** 사내 규정은 `policy_paragraph` 한 행(= 조 하나, 조 단위
   청킹), 개정 조문은 `ArticleUnit` 하나. 양쪽의 분모 단위가 같아야 두 비율을
   나란히 놓을 수 있다.

2. **조 표기는 `evals.runners.baseline.article_tokens()` 로 뽑는다.** B-1 이 쓰는
   바로 그 함수다. 다른 정규화를 쓰면 두 수를 비교할 수 없다(`docs/21` §7 지시).

3. **타법 인용** = 본문에 `「법령명」` 직후 **공백 0~1자** 뒤 조 표기가 오는 형태가
   한 번 이상 있는 것. `「전기통신기본법」 제2조제1호` 가 그 형태다.

4. **자문서 인용** = 본문의 조 표기 중 (a) 타법 인용에 쓰인 것과 (b) **자기 조 번호**
   를 뺀 나머지가 한 번 이상 남는 것. 개정 조문 조립본은 머리표기(`제1조(목적)`)를
   포함하므로 (b) 를 빼지 않으면 전건이 자기 자신을 인용한 것이 된다.

5. **밀도** = 조건을 만족하는 조 수 ÷ 전체 조 수. 조 하나가 인용을 몇 번 하든 1 로
   센다 — 사내 쪽 0.1447(22/152)이 그렇게 세어진 값이다.

──────────────────────────────────────────────────────────────────────────────
판정 기준 — **측정 전에 확정했다**
──────────────────────────────────────────────────────────────────────────────

귀무가설은 「두 밀도가 같다(p = 0.1447)」이다. 그 가정에서 두 비율 차의 표준오차는

    SE = sqrt( p(1-p) · (1/n_사내 + 1/n_개정) )

이고, **비슷하다 = |차| ≤ 2·SE**(약 95% 구간, 유의한 차이가 관측되지 않음)로 정한다.
n_사내 = 152, n_개정 = 112 이면 SE = 0.0438, 판정 폭은 **±0.0876** 이다.

    |차| ≤ 2SE          → 비슷하다        → 신규 27건이 운영에 가깝다 → 검색이 병목
    개정 - 사내 > 2SE   → 개정 쪽이 높다  → 기존 15건이 운영에 가깝다 → 선택이 병목
    개정 - 사내 < -2SE  → 많이 다르다     → **판단 보류**

2SE 를 고른 이유: 1SE(≈68%)는 「비슷하다」를 너무 좁게 잡아 표본 요동만으로 보류가
나오고, 3SE 는 반대로 어떤 차이도 비슷하다고 부른다. 관행적인 95% 구간이 이 판정에서
「유의한 차이가 없다」와 같은 뜻이 되므로 그것을 쓴다.

──────────────────────────────────────────────────────────────────────────────
이 측정의 한계 — **축소하지 않는다**
──────────────────────────────────────────────────────────────────────────────

**두 밀도는 같은 것을 재지 않는다.** 개정 조문의 인용 밀도는 「법이 법을 인용하는
빈도」이고 사내 규정의 인용 밀도는 「사내 규정이 법을 인용하는 빈도」다. 같은 척도로
잰다고 같은 것을 재는 것이 아니며, 이 대조로 §7 의 질문에 답이 완전히 나오지 않는다.

그래도 하는 이유는 **지금 우리에게 있는 유일한 외부 데이터**이기 때문이다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from evals.runners.b1_precheck import token_of
from evals.runners.baseline import article_tokens
from regchange.config.settings import apply_dotenv, snapshot_root
from regchange.parse import assemble_body, parse_law_document
from regchange.parse.law_xml import ParseError
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"

AS_OF = dt.date(2026, 2, 1)
"""사내 규정 시점. `docs/10-retrieval-evaluation-protocol.md` 규약과 같은 값이다 —
다른 시점을 쓰면 152 문단이라는 분모가 달라져 0.1447 과 비교할 수 없다."""

INTERNAL_DENSITY_PRIOR = 0.1447
"""`docs/21` §7 이 적은 사내 규정 인용 밀도(22/152). **이 러너가 다시 잰다.**

기록된 값을 그대로 쓰지 않는 이유: 0.1447 은 커밋된 코드 없이 잰 값이라 개정 조문
쪽과 **같은 함수로 세어졌다는 보장이 없다.** 재측정치가 이 값과 다르면 그 차이 자체가
관측이며, 판정에는 재측정치를 쓴다."""

SIMILARITY_SE_MULTIPLIER = 2.0
"""판정 폭 = 이 배수 곱하기 표준오차. 근거는 모듈 docstring 「판정 기준」."""

LAW_NAME_SUFFIXES = ("법", "법률", "시행령", "시행규칙", "규정", "규칙")
"""`「…」` 안의 이름이 **법령**인지 가르는 접미사.

확보된 스냅샷 전체의 `「…」` 166종을 전수 확인했고 100/55/11/2 가 각각
법·법률·시행령·규정으로 끝났다 — 그 밖의 접미사는 0 종이었다. 사내 코퍼스의
`「접근통제 절차서」`·`「정보보호정책」`·`「정보보호 관리지침」` 은 이 목록에
걸리지 않는다(절차서/정책/지침). **사내 문서를 법령으로 세지 않기 위한 경계다.**"""

ADJACENT_ARTICLE = re.compile(r"\s?(제\d+조(?:의\d+)?)")
"""`」` 바로 뒤에서 조 표기를 찾는다. 공백 0~1자만 허용한다.

실측 근거: 스냅샷 전체에서 `「…」` 뒤 40자 안에 조 표기가 오는 경우의 간격 분포는
0자 4건 · **1자 488건** · 13자 이상 27건이었다. 13자 이상은 `「…법」(이하 "법"이라
한다) 제2조` 이거나 아예 다른 문장이며, 둘 사이에 값이 없다. **임의로 고른 창이
아니라 분포가 비어 있는 자리에서 끊었다.**"""

BRACKETED_NAME = re.compile(r"「([^」]{1,60})」")
"""`「법령명」`. 60자 상한은 실측 최장 법령명(`외국인관광객 등에 대한 부가가치세 및
개별소비세 특례규정`, 33자)의 약 2배이며, 상한이 없으면 여는 괄호 하나가 문서 끝까지
먹는다."""

PARAGRAPH_QUERY = """
SELECT d.doc_id, p.article_no, p.text_raw
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
 WHERE p.known_until = 'infinity' AND d.known_until = 'infinity'
   AND d.effective_date <= %(as_of)s
 ORDER BY d.doc_id, p.seq_in_doc
"""

logger = logging.getLogger("citation_density")


@dataclass(frozen=True, slots=True)
class ArticleCitations:
    """조 하나의 인용 판정 결과.

    목적:
        「타법을 인용했는가」와 「같은 문서의 다른 조를 인용했는가」를 **따로** 들고
        다닌다.

    구현 이유:
        두 값을 하나의 불리언이나 개수로 합치면 §7 이 요구한 세 비율 중 셋째
        (「같은 법의 다른 조를 인용하는 비율」)를 낼 수 없다. 상태를 합치지 않는
        것은 `docs/19-state-conflation-audit.md` 가 네 번 무너졌다고 적은 그 규칙이다.

    트레이드오프:
        인용 **횟수**를 세지 않고 표기 집합만 든다. 0.1447 이 조 단위 유무로 세어진
        값이라 횟수를 세면 비교가 성립하지 않는다.

    엣지 케이스:
        - 타법 인용과 자문서 인용이 둘 다 있는 조: 두 집합 모두 비지 않는다.
          어느 한쪽으로 배타 분류하지 않는다 — 실제로 둘 다 하는 조가 있다.
        - 자기 조 번호만 나오는 조(조립본 머리표기): `internal` 이 빈 집합이다.
    """

    key: str
    other_law: frozenset[str]
    """`「법령명」 제N조` 형태로 인용된 조 표기."""

    internal: frozenset[str]
    """자기 조 번호와 타법 인용분을 뺀 나머지 조 표기."""

    group: str = ""
    """집계 축(개정 조문은 `제개정구분`). 빈 문자열은 「축이 없다」이며 「미상」이 아니다."""


def is_law_name(name: str) -> bool:
    """`「…」` 안의 이름이 법령명인가. 접미사 판정이며 근거는 `LAW_NAME_SUFFIXES`."""
    return name.strip().endswith(LAW_NAME_SUFFIXES)


def measure_text(key: str, text: str, self_token: str | None, group: str = "") -> ArticleCitations:
    """조 하나의 본문에서 인용을 뽑는다.

    목적:
        모듈 docstring 의 정의 3·4 를 그대로 코드로 옮긴다. **사내 규정과 개정 조문이
        이 한 함수를 지난다** — 두 밀도가 같은 기준으로 세어졌다는 것이 이 함수가
        하나뿐이라는 사실로 보장된다.

    구현 이유:
        조 표기 추출을 자체 정규식으로 하지 않고 `article_tokens()` 에 넘긴다.
        B-1 과 같은 함수를 쓰라는 것이 `docs/21` §7 의 지시이며, 범위·나열 표기
        (`제44조의2~5`)의 전개 규칙이 갈리면 두 수를 비교할 수 없다.

    트레이드오프:
        `「…법」에 따른 … 같은 법 제59조` 처럼 **법령명과 조 표기가 떨어진** 타법
        인용은 자문서 인용으로 오분류된다(스냅샷 전수에서 11건). 붙어 있는 형태만
        타법으로 세는 대신 정의가 한 줄로 끝나고, 오분류 방향이 **자문서 인용을
        부풀리고 타법 인용을 깎으므로** 「개정 쪽이 더 인용한다」는 결론에 불리하다.
        결론을 우리에게 유리하게 만드는 방향이 아니라서 허용한다.

    엣지 케이스:
        - `self_token` 이 None(사내 문단): 뺄 자기 조 번호가 없다. `text_raw` 에는
          머리표기가 들어 있지 않으므로 뺄 것이 애초에 없다.
        - `「정보보호 관리지침」(ISP-GUIDE-002) 제35조`: 접미사가 「지침」이라 법령이
          아니고, 간격도 1자를 넘어 타법 인용이 아니다. **사내 문서 상호참조는 자문서
          인용으로 센다** — 「법령을 인용했는가」가 질문이기 때문이다.
        - `「…법」` 뒤에 조 표기가 없는 인용(법령명만): 어느 쪽에도 세지 않는다.
          `docs/09` §5.3 이 「법령명만」을 조 단위 인용과 별도 칸으로 센 것과 같다.
        - 빈 문자열: 두 집합 모두 빈 집합. 예외를 던지지 않는다 — 본문이 비었다는
          것은 파서가 아니라 원문의 사실일 수 있다.
    """
    other: set[str] = set()
    for match in BRACKETED_NAME.finditer(text):
        if not is_law_name(match.group(1)):
            continue
        adjacent = ADJACENT_ARTICLE.match(text, match.end())
        if adjacent is not None:
            other.update(article_tokens(adjacent.group(1)))

    internal = set(article_tokens(text)) - other
    if self_token is not None:
        internal.discard(self_token)
    return ArticleCitations(
        key=key, other_law=frozenset(other), internal=frozenset(internal), group=group
    )


def summarize(name: str, rows: list[ArticleCitations]) -> dict[str, Any]:
    """조 목록을 세 비율로 접는다.

    분모를 함께 싣는 이유: 비율만 남기면 n=112 와 n=1,000 이 같은 무게로 읽힌다.
    """
    total = len(rows)
    other = sum(1 for r in rows if r.other_law)
    internal = sum(1 for r in rows if r.internal)
    both = sum(1 for r in rows if r.other_law and r.internal)
    neither = sum(1 for r in rows if not r.other_law and not r.internal)
    return {
        "name": name,
        "articles": total,
        "other_law_articles": other,
        "other_law_density": round(other / total, 4) if total else None,
        "internal_articles": internal,
        "internal_density": round(internal / total, 4) if total else None,
        "both": both,
        "neither": neither,
    }


def verdict(
    internal_density: float, amended_density: float, n_internal: int, n_amended: int
) -> dict[str, Any]:
    """모듈 docstring 의 판정 규칙을 적용한다. 규칙 자체는 여기서 정하지 않는다."""
    p = internal_density
    se = math.sqrt(p * (1 - p) * (1 / n_internal + 1 / n_amended))
    band = SIMILARITY_SE_MULTIPLIER * se
    diff = amended_density - internal_density
    if abs(diff) <= band:
        call = "SIMILAR"
    elif diff > band:
        call = "AMENDMENT_HIGHER"
    else:
        call = "AMENDMENT_LOWER"
    return {
        "internal_density": round(internal_density, 4),
        "amended_density": round(amended_density, 4),
        "diff": round(diff, 4),
        "standard_error": round(se, 4),
        "band": round(band, 4),
        "verdict": call,
    }


def latest_snapshot_dirs() -> dict[str, Path]:
    """MST 별로 **가장 최근** 스냅샷 디렉터리. 같은 MST 가 여러 실행에 걸쳐 있다."""
    latest: dict[str, Path] = {}
    for path in sorted(snapshot_root().glob("*/law/MST-*")):
        mst = path.name.split("-")[1]
        latest[mst] = path
    return latest


def collect_amended(
    events: list[dict[str, Any]],
) -> tuple[list[ArticleCitations], list[dict[str, str]]]:
    """개정 이벤트가 **변경으로 표시한 조문**만 훑는다.

    목적:
        「개정 조문」의 인용 밀도를 낸다. 스냅샷의 모든 조를 세면 그것은 개정 조문이
        아니라 법 조문 일반이다.

    구현 이유:
        본문을 `assemble_body()` 로 읽는다. 파서 중간값(`조문내용`)을 직접 읽어
        없는 발견을 만든 사례가 있다
        (`docs/incidents/measurement-reported-failure-as-success.md` §5-1).

    트레이드오프:
        변경 조문 목록이 있는 원천만 셀 수 있다 — 골든 42건이 쓰는 나머지 원천
        (285199·283839 등)은 어느 조가 개정됐는지 기록이 없어 분모에 넣지 못한다.
        그 대가로 **분모가 「개정 조문」이라는 것이 보장된다.**

    엣지 케이스:
        - `after_snapshot` 이 None: **그 MST 의 최근 스냅샷으로 대체한다.** MST 가 법령
          버전을 식별하므로 어느 실행이 받아왔든 본문은 같다. 대체한 사실은
          `snapshot_fallback` 으로 남긴다 — 조용히 대체하면 「수집이 성공했다」와
          구별되지 않는다. `b1_precheck` 도 같은 경로로 112조를 훑었고, 대체하지
          않으면 그 측정과 분모가 달라진다(90 vs 112).
        - 대체할 스냅샷도 없음: 실패로 기록한다. 조용히 건너뛰면 「인용 0」과
          구별되지 않는다.
        - 파싱 실패: 같은 이유로 실패 목록에 남긴다.
        - 캐시가 변경으로 표시한 조가 본문에 없음: 남은 조만 세고 실패에 사유를
          적는다. `b1_precheck` 가 같은 자리에서 같은 구별을 한다.
        - 같은 조가 여러 페이지에 중복 등장: `(mst, article_key)` 로 중복 제거한다.
    """
    rows: dict[str, ArticleCitations] = {}
    failures: list[dict[str, str]] = []
    fallbacks = latest_snapshot_dirs()
    for event in events:
        mst = str(event["mst"])
        snapshot = event.get("after_snapshot")
        directory = snapshot_root() / str(snapshot) if snapshot else fallbacks.get(mst)
        if directory is None:
            failures.append({"mst": mst, "reason": "after 스냅샷 없음 · 대체 스냅샷도 없음"})
            continue
        if not snapshot:
            failures.append(
                {"mst": mst, "reason": f"snapshot_fallback: {directory.name} 으로 대체"}
            )
        pages = sorted(directory.glob("page-*.xml"))
        if not pages:
            failures.append({"mst": mst, "reason": "스냅샷 디렉터리에 페이지 없음"})
            continue
        changed = {(int(str(k).zfill(6)[:4]), int(str(k).zfill(6)[4:6])) for k in event["articles"]}
        seen = 0
        for page in pages:
            try:
                document = parse_law_document(page)
            except ParseError as exc:
                failures.append({"mst": mst, "reason": f"파싱 실패: {exc}"})
                continue
            for unit in document.articles:
                if (unit.article_no, unit.branch_no) not in changed:
                    continue
                key = f"{mst}#{unit.article_key}"
                if key in rows:
                    continue
                token = token_of(unit.article_no, unit.branch_no)
                rows[key] = measure_text(
                    key, assemble_body(unit).raw, token, str(event["revision_kind"])
                )
                seen += 1
        if seen < len(changed):
            failures.append(
                {"mst": mst, "reason": f"변경 표시 {len(changed)}조 중 본문에서 찾은 것 {seen}조"}
            )
    return list(rows.values()), failures


def collect_all_articles() -> tuple[list[ArticleCitations], list[dict[str, str]]]:
    """확보된 스냅샷의 **모든 조문**. 보조 지표다.

    목적:
        「개정 조문」이 아니라 「법 조문 일반」의 인용 밀도를 함께 낸다. 개정 조문
        112개보다 분모가 크고, 골든 42건이 쓰는 원천들이 여기에는 들어온다.

    구현 이유:
        `docs/21` §7 이 범위 후보로 「확보된 스냅샷 전체」를 함께 적었다. 둘을 나란히
        내면 「개정된 조문이라서 인용이 많다/적다」와 「법 조문이 원래 그렇다」가
        갈린다.

    트레이드오프:
        개정 조문 112개를 포함하는 **상위 집합**이므로 두 수가 독립이 아니다. 판정에
        쓰지 않고 참고로만 싣는 이유가 이것이다.

    엣지 케이스:
        - 같은 MST 가 여러 실행에 있음: 가장 최근 디렉터리만 쓴다.
        - 파싱 실패: 실패 목록에 남기고 그 MST 는 분모에서 빠진다.
    """
    rows: list[ArticleCitations] = []
    failures: list[dict[str, str]] = []
    for mst, directory in sorted(latest_snapshot_dirs().items()):
        for page in sorted(directory.glob("page-*.xml")):
            try:
                document = parse_law_document(page)
            except ParseError as exc:
                failures.append({"mst": mst, "reason": f"파싱 실패: {exc}"})
                continue
            for unit in document.articles:
                key = f"{mst}#{unit.article_key}"
                token = token_of(unit.article_no, unit.branch_no)
                rows.append(measure_text(key, assemble_body(unit).raw, token))
    return rows, failures


async def collect_internal() -> list[ArticleCitations]:
    """사내 규정 문단을 같은 함수로 다시 센다 (`INTERNAL_DENSITY_PRIOR` 재측정)."""
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        cur = await conn.execute(PARAGRAPH_QUERY, {"as_of": AS_OF})
        paragraphs = [(str(d), int(n), str(t)) for d, n, t in await cur.fetchall()]
    return [measure_text(f"{doc}#{no}", text, f"제{no}조") for doc, no, text in paragraphs]


async def run(sources_file: Path, events: list[dict[str, Any]]) -> None:
    apply_dotenv()

    internal_rows = await collect_internal()
    amended_rows, amended_failures = collect_amended(events)
    all_rows, all_failures = collect_all_articles()

    internal = summarize("사내 규정 (policy_paragraph)", internal_rows)
    amended = summarize("개정 조문 (변경 표시된 조)", amended_rows)
    every = summarize("스냅샷 전체 조문 (참고)", all_rows)

    by_doc: Counter[str] = Counter()
    for row in internal_rows:
        if row.other_law:
            by_doc[row.key.split("#")[0]] += 1

    report: dict[str, Any] = {
        "measured_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "as_of": AS_OF.isoformat(),
        "sources_file": sources_file.name,
        "definition": {
            "unit": "조 1개",
            "token_extractor": "evals.runners.baseline.article_tokens",
            "other_law": "「법령명」 + 공백 0~1자 + 조 표기",
            "internal": "본문 조 표기 - 타법 인용분 - 자기 조 번호",
            "law_name_suffixes": list(LAW_NAME_SUFFIXES),
        },
        "internal": internal | {"other_law_by_doc": dict(by_doc)},
        "internal_density_prior": INTERNAL_DENSITY_PRIOR,
        "amended": amended
        | {
            "failures": amended_failures,
            # **타법개정은 인용 조문 번호만 바뀌는 것이 많다**(`b1_precheck` 모듈
            # docstring). 그 성질이 타법 인용 밀도를 끌어올릴 수 있으므로 나눠 싣는다.
            # 판정에는 전체 112조를 쓴다 — 축을 나눠 유리한 쪽을 고르지 않는다.
            "by_revision_kind": [
                summarize(kind, [r for r in amended_rows if r.group == kind])
                for kind in sorted({r.group for r in amended_rows})
            ],
        },
        "all_articles": every | {"failures": all_failures},
        "verdict": verdict(
            float(internal["other_law_density"]),
            float(amended["other_law_density"]),
            int(internal["articles"]),
            int(amended["articles"]),
        ),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"citation-density-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for line in (internal, amended, every):
        logger.info(
            "%-30s 조 %4d | 타법 인용 %3d (%s) | 자문서 인용 %3d (%s) | 둘 다 %3d | 없음 %3d",
            line["name"],
            line["articles"],
            line["other_law_articles"],
            line["other_law_density"],
            line["internal_articles"],
            line["internal_density"],
            line["both"],
            line["neither"],
        )
    logger.info(
        "사내 기록값(docs/21 §7) %s vs 재측정 %s",
        INTERNAL_DENSITY_PRIOR,
        internal["other_law_density"],
    )
    logger.info("판정: %s", json.dumps(report["verdict"], ensure_ascii=False))
    logger.info("결과: %s", out)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="인용 밀도 대조 (docs/21 §7)")
    parser.add_argument("--sources", type=Path, default=None)
    args = parser.parse_args()
    sources: Path = args.sources or sorted(RESULTS_DIR.glob("expansion-sources-*.json"))[-1]
    events: list[dict[str, Any]] = json.loads(sources.read_text(encoding="utf-8"))["events"]
    asyncio.run(run(sources, events))


if __name__ == "__main__":
    main()
