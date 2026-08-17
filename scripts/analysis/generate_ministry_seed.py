"""조직 마스터 초기 데이터(마이그레이션 004)를 0.8 캐시에서 생성한다.

목적:
    `data/frequency-cache` 365일 응답에서 `ministry_master` 초기 행을 뽑아
    `db/migrations/004_ministry_master_seed.sql` 을 만든다.

구현 이유:
    시드를 손으로 쓰지 않는다. 수십 개 부처를 손으로 옮기면 오타가 생기고, 그 오타는
    부처 하나가 조용히 미해결이 되는 형태로만 드러난다. 생성 스크립트를 남기면
    "이 숫자가 어디서 왔는가"에 재실행으로 답할 수 있다.

    짝짓기는 `regchange.store.ministry.extract_pair` 를 그대로 쓴다. 시드 생성이
    자기만의 짝짓기 규칙을 갖는 순간, 위치 zip 금지가 한쪽에서만 지켜진다.

    uuid 는 uuid5 로 결정론적으로 만든다. 재생성했을 때 파일이 바이트 단위로 같아야
    마이그레이션 해시 검사가 "수정됨"으로 오탐하지 않는다.

트레이드오프:
    캐시가 사라지면 재생성할 수 없다. 캐시를 커밋하지 않기로 한 결정의 결과이며,
    그래서 생성된 SQL 파일 자체를 커밋한다 — SQL 이 원본이고 스크립트는 그 유래를
    기록하는 것이다.

엣지 케이스:
    - 복수 나열 행(615건): 대응을 만들지 않는다. 시드에 들어가지 않는다.
    - 같은 코드에 이름이 2개 이상: 실측 0건이다. 관측되면 경고를 출력하고 첫
      관측을 쓴다 — 시드가 조용히 둘 중 하나를 고르지 않게 한다.
    - 캐시가 없는 경우: 실패한다. 빈 시드를 만들지 않는다.
"""

from __future__ import annotations

import datetime as dt
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from regchange.store.ministry import (
    MinistryObservation,
    extract_pair,
)

CACHE_DIR = ROOT / "data" / "frequency-cache"
OUTPUT = ROOT / "db" / "migrations" / "004_ministry_master_seed.sql"

OBSERVED_AT = dt.date(2026, 8, 11)
"""0.8 캐시를 수집한 날. 관측된 이름은 이 시점부터 유효한 것으로 둔다.

캐시 파일 mtime 이 전부 2026-08-11 이고 ADR-009·amendment-frequency.md 의 측정일과
같다. **이 날짜 이전으로 소급하지 않는다** — API 가 과거 행에도 현재 이름을 넣어
돌려주므로, 관측값을 과거로 소급하면 그 시점에 없던 이름을 재현하게 된다.
"""

KNOWN_FROM = "2026-08-11T00:00:00+00:00"

WATCHED_KINDS = (
    "환경부령",
    "기후에너지환경부령",
    "산업통상자원부령",
    "산업통상부령",
    "여성가족부령",
    "성평등가족부령",
    "기획재정부령",
    "재정경제부령",
)
"""이름 변경 경계가 관측된 `법령구분명` 값들 (ADR-009 §맥락).

이 값들은 부처명이 아니라 **법령 종류**다. `환경부령` 에서 `령` 을 떼어 `환경부` 를
만드는 추론을 하지 않는다 — `대통령령`·`총리령` 에 같은 규칙을 적용하면 부처가
아닌 것이 부처가 된다. 관측된 문자열을 그대로 두고 `source_field` 로 구별한다.
"""


def _quote(value: str | None) -> str:
    """SQL 문자열 리터럴. 작은따옴표만 이스케이프하면 충분하다 (부처명에 한정)."""
    if value is None:
        return "NULL"
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _as_date(yyyymmdd: str) -> dt.date:
    """`20251001` 형태를 date 로 바꾼다. strptime 은 naive datetime 을 만들므로 쓰지 않는다."""
    return dt.date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))


def _row_id(source: str, code: str | None, name: str, valid_from: dt.date) -> str:
    return str(uuid5(NAMESPACE_URL, f"ministry_master/{source}/{code}/{name}/{valid_from}"))


def main() -> int:
    if not CACHE_DIR.is_dir():
        print(f"캐시 디렉터리가 없다: {CACHE_DIR}", file=sys.stderr)
        return 1

    pairs: dict[str, str] = {}
    conflicts: dict[str, set[str]] = defaultdict(set)
    kind_range: dict[str, list[str]] = {}
    multi_rows = 0
    total_rows = 0

    for path in sorted(CACHE_DIR.glob("*.xml")):
        for info in ET.parse(path).getroot().iter("법령정보"):
            total_rows += 1
            observation = MinistryObservation(
                code_field=info.findtext("소관부처코드"),
                name_field=info.findtext("소관부처명"),
            )
            pair = extract_pair(observation)
            if pair is None:
                multi_rows += 1
            else:
                code, name = pair
                conflicts[code].add(name)
                pairs.setdefault(code, name)

            kind = (info.findtext("법령구분명") or "").strip()
            day = (info.findtext("공포일자") or "").strip()
            if kind in WATCHED_KINDS and day:
                span = kind_range.setdefault(kind, [day, day])
                span[0] = min(span[0], day)
                span[1] = max(span[1], day)

    for code, names in sorted(conflicts.items()):
        if len(names) > 1:
            print(f"경고: 코드 {code} 에 이름이 여러 개다 {sorted(names)}", file=sys.stderr)

    lines: list[str] = []
    lines.append(_header(total_rows, multi_rows, len(pairs), kind_range))

    lines.append("-- OBSERVED_FLATTENED — (소관부처코드, 소관부처명) 단일값 행에서만 얻은 대응")
    lines.append(
        "INSERT INTO ministry_master "
        "(id, org_code, org_name, source_field, source, valid_from, valid_until, "
        "known_from, note) VALUES"
    )
    values: list[str] = []
    for code, name in sorted(pairs.items()):
        row_id = _row_id("OBSERVED_FLATTENED", code, name, OBSERVED_AT)
        values.append(
            f"    ('{row_id}', {_quote(code)}, {_quote(name)}, '소관부처명', "
            f"'OBSERVED_FLATTENED', DATE '{OBSERVED_AT}', NULL, "
            f"TIMESTAMPTZ '{KNOWN_FROM}', "
            "'0.8 캐시 관측. API 가 과거 행에도 현재 이름을 반환하므로 소급하지 않는다')"
        )
    lines.append(",\n".join(values) + ";")
    lines.append("")

    lines.append("-- OBSERVED_BOUNDARY — 법령구분명에서 실측된 이름 변경 경계")
    lines.append(
        "INSERT INTO ministry_master "
        "(id, org_code, org_name, source_field, source, valid_from, valid_until, "
        "known_from, note) VALUES"
    )
    boundary_values: list[str] = []
    for kind in WATCHED_KINDS:
        span = kind_range.get(kind)
        if span is None:
            print(f"경고: 법령구분명 {kind} 가 캐시에 없다", file=sys.stderr)
            continue
        first = _as_date(span[0])
        # valid_until 을 항상 NULL 로 둔다. 우리가 관측한 것은 "이 값이 이 날부터
        # 나타났다"이고, 마지막 관측일은 **종료일이 아니다** — 그 뒤로 그 종류의
        # 법령이 개정되지 않았을 뿐일 수 있다. 마지막 관측일 + 1일을 valid_until 로
        # 쓰면 관측하지 않은 종료를 주장하게 된다.
        # 이름 변경 경계는 새 값의 valid_from 에 그대로 담긴다 (2025-10-01, 2026-01-02).
        row_id = _row_id("OBSERVED_BOUNDARY", None, kind, first)
        boundary_values.append(
            f"    ('{row_id}', NULL, {_quote(kind)}, '법령구분명', "
            f"'OBSERVED_BOUNDARY', DATE '{first}', NULL, "
            f"TIMESTAMPTZ '{KNOWN_FROM}', "
            f"'법령구분명 관측 구간 {span[0]}~{span[1]}. {span[1]} 은 마지막 관측일이지 "
            f"종료일이 아니다. 부처명이 아니며 org_code 미상')"
        )
    lines.append(",\n".join(boundary_values) + ";")
    lines.append("")

    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"{OUTPUT} 생성: 평탄화 {len(pairs)}행 + 경계 {len(boundary_values)}행")
    print(f"  전체 행 {total_rows}, 복수 나열로 제외 {multi_rows}")
    return 0


def _header(total: int, multi: int, pair_count: int, kind_range: dict[str, list[str]]) -> str:
    ranges = "\n".join(
        f"--     {kind:<16} {span[0]}~{span[1]}" for kind, span in sorted(kind_range.items())
    )
    single = total - multi
    return f"""-- 004 — 조직 마스터 초기 데이터 (ADR-009)
--
-- **생성된 파일이다. 손으로 고치지 않는다.**
-- 생성: scripts/analysis/generate_ministry_seed.py
-- 원본: data/frequency-cache (2025-08-01~2026-07-31, 관측일 {OBSERVED_AT})
--
-- 목적:
--   부서 배정이 참조할 소관부처 코드/명칭을 데이터로 둔다. 하드코딩하지 않는다.
--
-- 구현 이유:
--   두 종류를 `source` 로 구별해 함께 넣는다. 하나만 넣으면 각각 다른 방식으로
--   틀린다.
--
--   OBSERVED_FLATTENED ({pair_count}행) — (소관부처코드, 소관부처명) 대응.
--     **전부 '현재 이름'이다.** 단일값 행 {single}건에서 코드 {pair_count}개를 얻었는데
--     그중 **이름이 2개 이상인 코드는 0개**다. 같은 캐시의 법령구분명은
--     환경부령(~20250930) / 기후에너지환경부령(20251001~) 로 이름 변경을 선명하게
--     보여주는데도 그렇다. API 가 과거 행에도 오늘의 이름을 넣어 돌려주기 때문이며,
--     본문 API 가 조문별 시행일을 문서 시행일로 평탄화하는 것과 같은 기전이다.
--     그래서 valid_from 을 관측일({OBSERVED_AT})로 두고 **과거로 소급하지 않는다.**
--     그 이전 시점 조회는 "해당 시점 이름 미상"으로 답하는 것이 옳다.
--
--     이 {pair_count}개는 전 부처가 아니다. 복수 나열 행에서만 등장하는 코드는 이름을
--     얻을 수 없어 빠져 있고, 그 문서들은 적재 시 미해결로 기록된다.
--
--   OBSERVED_BOUNDARY — 법령구분명에서 실측된 이름 변경 경계. 시점은 정확하지만
--     org_code 가 없다. 코드를 붙이려면 조직 동일성 판단이 필요하고 그 판단은
--     사람이 한다 (ADR-009: 자동 병합하지 않는다).
--     **valid_until 은 전부 NULL 이다.** 관측된 것은 "이 값이 이 날부터 나타났다"이지
--     "이 날 끝났다"가 아니다. 마지막 관측일 다음 날로 닫으면 관측하지 않은 종료를
--     주장하게 된다. 이름 변경 경계일(2025-10-01, 2026-01-02)은 새 값의 valid_from
--     으로 담긴다.
--
-- 트레이드오프:
--   복수 나열 행 {multi}건(전체 {total}건)을 통째로 버린다. 코드 개수와 이름 개수는
--   항상 일치하지만 위치로 짝지으면 212건(34.5%)이 어긋난다 — 예:
--     환경부와 그 소속기관 직제(20250923) 코드 1741000,1482000 / 이름 기후에너지환경부,행정안전부
--   길이가 맞으므로 어떤 단언에도 걸리지 않고 403건은 우연히 맞는다. 정보를 버리는
--   대신 조용한 오배정을 만들지 않는다.
--
-- 엣지 케이스:
--   - 경계 구간이 캐시 마지막 날까지 이어지면 valid_until 을 NULL(현재)로 둔다.
--   - 기획재정부령(~2025-12-31)과 재정경제부령(2026-01-02~) 사이 하루는 관측이
--     없다. 관측되지 않은 구간을 메우지 않는다 — 경계일을 지어내는 것이 된다.
--   - 관측된 법령구분명 구간:
{ranges}
--
-- 재실행: 이 파일은 마이그레이션이므로 한 번만 적용된다. 마스터를 갱신하려면
--         새 마이그레이션을 추가한다 (원칙 6: 과거 행을 UPDATE 하지 않는다).
"""


if __name__ == "__main__":
    raise SystemExit(main())
