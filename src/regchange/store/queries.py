"""시점 질의 3종 — 현재 시행 · 시행 예정 · 감사 재현.

목적:
    bitemporal 로 쌓인 조문에서 "지금 무엇이 유효한가", "무엇이 곧 시행되는가",
    그리고 **"그 시점에 담당자가 볼 수 있었던 것은 무엇인가"**에 답한다.

구현 이유:
    세 질의를 문자열 상수로 노출한다. 감사에서 "이 화면의 숫자가 어떤 기준으로
    나왔는가"를 물으면 답해야 할 것이 WHERE 절 그 자체이기 때문이다. ORM 이
    만들어 준 SQL 을 감사에 제출할 수는 없다.

    세 번째 질의가 이 프로젝트에서 가장 값비싸다. F-3(소명 불가)을 막는 것이
    이 시스템의 핵심 가치이며, 그 값은 **네 개의 시간 조건이 모두 걸릴 때만**
    나온다. `known_from <= t < known_until` 을 빠뜨리면 그 시점 이후에 알게 된
    정정이 과거 화면에 섞이고, 재현이 재현이 아니게 된다.

트레이드오프:
    `valid_from IS NULL` 인 조문은 **세 질의 어디에도 잡히지 않는다.** 본문만
    적재한 현재 상태에서는 그것이 전부다. 이 성질은 의도된 것이지만 그대로 두면
    "질의 결과가 0건"과 "데이터가 없음"이 구별되지 않는 조용한 누락이 된다.
    그래서 `count_pending_valid_from()` 을 함께 제공하고, 호출부가 두 숫자를
    나란히 보게 한다. 질의를 느슨하게 만들어 NULL 을 포함시키는 대안은 택하지
    않았다 — 시행일을 모르는 조문을 "오늘 시행 중"이라고 답하는 것이 된다.

    복합 인덱스를 (valid_from, valid_to, known_from, known_until) 하나로 뒀다.
    질의마다 최적 인덱스를 따로 만들면 쓰기 비용이 오르고, 현재 데이터 규모에서는
    한 인덱스로 충분하다. 규모가 커져 질의 3이 느려지면 그때 분리한다.

엣지 케이스:
    - 정정이 있었던 조문: 같은 조문에 닫힌 행과 열린 행이 함께 있다. 질의 3은
      **그 시점에 열려 있던 행**을 반환해야 하며, 지금 열린 행이 아니다.
    - `valid_to` 기본값 9999-12-31: 종료가 없는 조문이다. 배타적 끝으로 비교하므로
      그날 하루는 유효하지 않게 다뤄진다. 실무상 영향이 없는 값이다.
    - 조회 시점이 `known_from` 과 정확히 같은 순간: 포함한다(`<=`). 수집된 그
      순간부터 담당자가 볼 수 있었다.
    - `unit_type='HEADING'`: 편장절관 제목행은 인용 대상이 아니다(ADR-001).
      질의는 조문 본체만 돌려준다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.rows import dict_row

_SELECT_COLUMNS = """
    a.id, a.document_id, a.article_key, a.seq_in_doc,
    a.article_no, a.branch_no, a.title,
    a.text_raw, a.text_norm,
    a.valid_from, a.valid_to, a.valid_from_source,
    a.known_from, a.known_until,
    d.law_id, d.law_name, d.mst
"""

CURRENTLY_EFFECTIVE_SQL = f"""
SELECT {_SELECT_COLUMNS}
  FROM regulation_article a
  JOIN regulation_document d ON d.id = a.document_id
 WHERE a.unit_type = 'ARTICLE'
   AND a.valid_from <= %(as_of)s
   AND a.valid_to   >  %(as_of)s
   AND a.known_until = 'infinity'
 ORDER BY d.law_id, a.seq_in_doc
"""
"""질의 1 — 지금 시행 중이고 우리가 지금 아는 것."""

PENDING_EFFECTIVE_SQL = f"""
SELECT {_SELECT_COLUMNS}
  FROM regulation_article a
  JOIN regulation_document d ON d.id = a.document_id
 WHERE a.unit_type = 'ARTICLE'
   AND a.valid_from > %(as_of)s
   AND a.known_until = 'infinity'
 ORDER BY a.valid_from, d.law_id, a.seq_in_doc
"""
"""질의 2 — 시행 예정 (D-day 큐). `valid_from` 순으로 정렬해 그대로 큐가 된다."""

AS_KNOWN_AT_SQL = f"""
SELECT {_SELECT_COLUMNS}
  FROM regulation_article a
  JOIN regulation_document d ON d.id = a.document_id
 WHERE a.unit_type = 'ARTICLE'
   AND a.valid_from  <= %(valid_at)s
   AND a.valid_to    >  %(valid_at)s
   AND a.known_from  <= %(known_at)s
   AND a.known_until >  %(known_at)s
 ORDER BY d.law_id, a.seq_in_doc
"""
"""질의 3 — 감사 재현. **네 조건이 모두 걸려야 한다.**

`known_until = 'infinity'` 로 바꾸면 그 시점 이후에 알게 된 정정이 과거 화면에
섞인다. 그 오류는 예외가 아니라 "그럴듯하지만 그때 볼 수 없었던 조문"으로
나타나므로 눈으로 잡히지 않는다.
"""

PENDING_VALID_FROM_SQL = """
SELECT count(*) AS pending
  FROM regulation_article
 WHERE unit_type = 'ARTICLE'
   AND valid_from IS NULL
   AND known_until = 'infinity'
"""
"""시점 질의 어디에도 잡히지 않는 조문 수. 0건과 "아직 결합 안 됨"을 구별한다."""


async def currently_effective(
    conn: psycopg.AsyncConnection[Any],
    *,
    as_of: dt.date,
) -> list[dict[str, Any]]:
    """지금 시행 중이고 우리가 지금 아는 조문."""
    return await _run(conn, CURRENTLY_EFFECTIVE_SQL, {"as_of": as_of})


async def pending_effective(
    conn: psycopg.AsyncConnection[Any],
    *,
    as_of: dt.date,
) -> list[dict[str, Any]]:
    """아직 시행 전인 조문. 시행일 순으로 돌려주므로 D-day 큐로 쓴다."""
    return await _run(conn, PENDING_EFFECTIVE_SQL, {"as_of": as_of})


async def as_known_at(
    conn: psycopg.AsyncConnection[Any],
    *,
    valid_at: dt.date,
    known_at: dt.datetime,
) -> list[dict[str, Any]]:
    """`known_at` 시점에 담당자가 볼 수 있었던, `valid_at` 에 시행 중이던 조문.

    목적:
        감사 질문 "그 시점에 담당자가 알 수 있었던 정보는 무엇이었는가"에 답한다.

    구현 이유:
        두 시간 인자를 분리해 받는다. 하나로 합치면 "2026-03-15에 시행 중이던 조문을
        지금 시점에서 본다"와 "2026-03-15에 알고 있던 것"을 구별할 수 없다. 감사가
        묻는 것은 후자이고, 전자는 사후 지식으로 과거를 재구성한 것이라 답이 되지
        않는다.

    트레이드오프:
        호출부가 두 시각을 모두 넘겨야 해서 실수 여지가 있다. 기본값으로 하나를
        다른 하나에서 파생시키지 않은 이유는, 그 파생이 곧 두 시간축을 합치는
        일이기 때문이다.

    엣지 케이스:
        - 정정이 있었던 조문: 그 시점에 열려 있던 행을 돌려준다. 지금 열린 행이
          아니다. 이 구별이 이 함수가 존재하는 이유다.
        - 그 시점에 아직 수집하지 않은 조문: 결과에 없다. 없는 것이 정답이다.
    """
    return await _run(conn, AS_KNOWN_AT_SQL, {"valid_at": valid_at, "known_at": known_at})


async def count_pending_valid_from(conn: psycopg.AsyncConnection[Any]) -> int:
    """`valid_from` 이 아직 없는 조문 수.

    시점 질의 3종은 이 조문들을 반환하지 않는다. 숫자를 함께 보지 않으면
    "결과 0건"이 "데이터 없음"으로 읽힌다.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(PENDING_VALID_FROM_SQL)
        row = await cur.fetchone()
    return 0 if row is None else int(row["pending"])


async def explain(
    conn: psycopg.AsyncConnection[Any],
    sql: str,
    params: dict[str, Any],
) -> str:
    """질의 계획을 문자열로 돌려준다. 인덱스를 실제로 타는지 테스트가 확인한다."""
    async with conn.cursor() as cur:
        await cur.execute(f"EXPLAIN {sql}", params)
        rows = await cur.fetchall()
    return "\n".join(str(row[0]) for row in rows)


async def _run(
    conn: psycopg.AsyncConnection[Any],
    sql: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(row) for row in rows]
