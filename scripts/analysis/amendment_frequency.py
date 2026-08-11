"""법령 개정 빈도를 날짜 구간으로 수집·집계한다 (0.8단계 도메인 선택 근거).

목적:
    `lsJoHstInf`(lawSearch.do)를 하루 단위로 호출해 "그날 조문이 개정된 법령과
    조문"을 모으고, 법령별·부처별·월별·변경사유별로 집계한다. 나중에 우선순위 큐
    부하 산정과 운영 대시보드 기준선에 다시 쓴다.

구현 이유:
    수집(collect)과 집계(aggregate)를 분리했다. 수집은 네트워크에 의존하고 느리며
    한 번만 하면 되는 반면, 집계 기준은 여러 번 바뀐다. 붙여 두면 집계 로직을 고칠
    때마다 API를 다시 때리게 된다. 캐시를 파일로 두는 것도 같은 이유다 —
    재실행이 무료여야 집계를 자유롭게 고칠 수 있다.

트레이드오프:
    캐시 디렉터리가 수백 MB까지 커질 수 있고 커밋 대상이 아니므로, 다른 사람이
    같은 결과를 재현하려면 수집을 다시 돌려야 한다(약 8분). 재현성을 일부 포기한
    대신 저장소를 가볍게 유지했다. 집계 결과(CSV)는 커밋하므로 숫자 자체는 검증
    가능하다.

엣지 케이스:
    - `lsJoHstInf`는 성공 응답에도 `resultCode`가 없다. "정상 0건"과 "요청 실패"의
      응답이 바이트 단위로 동일하다. 이 측정에서 실패를 0으로 세면 결론 전체가
      틀어지므로 두 가지로 방어한다: (1) 0건인 날은 한 번 재요청해 같은 결과인지
      확인하고, (2) 30일마다 알려진 비-0 날짜(카나리아)를 호출해 세션이 살아
      있는지 확인한다. `lsHstInf`는 교차 확인에 쓸 수 없다 — 그쪽 `regDt`는
      공포일이 아니라 법제처 DB 등록일이라 날짜 의미가 다르다(실측).
    - 응답 형태를 분류해 빈 본문·HTML·XML 파싱 실패를 각각 실패로 기록한다.
      실패한 날짜는 집계에서 빼고 리포트에 명시한다.
    - `totalCnt`는 조문 수가 아니라 **법령 수**다. 수신한 `<law>` 수와 대조해
      잘림을 검출하고, 부족하면 페이지를 넘긴다.
    - OC는 응답 본문에 echo되므로 캐시에 쓰기 전에 마스킹한다. 마스킹 실패 시
      저장하지 않는다.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "exploration"))

from fetch import BASE, MIN_DELAY_SECONDS, OC, scrub

CACHE_DIR = REPO_ROOT / "data" / "frequency-cache"
META_PATH = REPO_ROOT / "data" / "frequency-cache" / "_meta.json"

CANARY_DATE = "20250401"
"""0.7단계에서 totalCnt=83으로 확인된 날. 측정 구간 밖이라 집계를 오염시키지 않는다."""

CANARY_EXPECTED_MIN = 1
"""카나리아가 이 값 미만이면 세션이 깨진 것으로 본다."""

CANARY_EVERY_DAYS = 30
"""카나리아 호출 주기. 365일이면 13회로 전체 호출의 3% 미만이다."""

REQUEST_TIMEOUT_SECONDS = 60.0
"""대형 응답(하루 581개 법령 관측)이 있어 탐색용 30초보다 길게 잡는다."""

PAGE_SIZE = 1000
"""활용가이드상 display 최대는 100이나 실측 1000까지 반환된다(spec §7.1).
문서화되지 않은 동작이므로 잘림 검출(totalCnt 대조)을 반드시 함께 쓴다."""

MAX_PAGES = 20
"""무한 루프 방지. 1000 * 20 = 20,000 법령/일이면 이미 비정상이다."""

_last_call_at = 0.0


class ResponseKind(StrEnum):
    """응답 형태 분류. HTTP 상태코드로는 실패를 구별할 수 없다(edge-case #10)."""

    OK = "OK"
    EMPTY_BODY = "EMPTY_BODY"
    HTML = "HTML"
    UNPARSEABLE = "UNPARSEABLE"
    UNEXPECTED_ROOT = "UNEXPECTED_ROOT"


def classify(text: str) -> ResponseKind:
    """응답 본문을 형태로 분류한다. 0건 여부는 판단하지 않는다."""
    if not text.strip():
        return ResponseKind.EMPTY_BODY
    head = text.lstrip()[:200].lower()
    if head.startswith("<!doctype html") or "<html" in head:
        return ResponseKind.HTML
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ResponseKind.UNPARSEABLE
    if root.tag != "LawSearch":
        return ResponseKind.UNEXPECTED_ROOT
    return ResponseKind.OK


def _throttled_get(params: dict[str, str]) -> httpx.Response:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_DELAY_SECONDS:
        time.sleep(MIN_DELAY_SECONDS - elapsed)
    response = httpx.get(
        f"{BASE}/lawSearch.do",
        params={"OC": OC, **params},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    _last_call_at = time.monotonic()
    return response


def _write_cache(path: Path, text: str) -> None:
    body = scrub(text)
    if OC in body:
        raise RuntimeError(f"OC 마스킹 실패: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def fetch_day(day: date, *, refresh: bool = False) -> str:
    """하루치 조문 개정 이력을 받아 캐시에 저장하고 본문을 반환한다.

    페이지네이션: `totalCnt`(법령 수)와 수신한 `<law>` 수를 대조해 부족하면 다음
    페이지를 요청하고, 결과를 하나의 `<LawSearch>`로 합쳐 캐시에 쓴다.
    """
    stamp = day.strftime("%Y%m%d")
    cache_path = CACHE_DIR / f"{stamp}.xml"
    if cache_path.exists() and not refresh:
        return cache_path.read_text(encoding="utf-8")

    first = _throttled_get(
        {"target": "lsJoHstInf", "type": "XML", "regDt": stamp, "display": str(PAGE_SIZE)}
    )
    text = first.text
    if classify(text) is not ResponseKind.OK:
        _write_cache(cache_path, text)
        return scrub(text)

    root = ET.fromstring(text)
    total = int(root.findtext("totalCnt") or 0)
    laws = root.findall("law")
    page = 1
    while len(laws) < total and page < MAX_PAGES:
        page += 1
        nxt = _throttled_get(
            {
                "target": "lsJoHstInf",
                "type": "XML",
                "regDt": stamp,
                "display": str(PAGE_SIZE),
                "page": str(page),
            }
        )
        if classify(nxt.text) is not ResponseKind.OK:
            break
        more = ET.fromstring(nxt.text).findall("law")
        if not more:
            break
        for law in more:
            root.append(law)
        laws = root.findall("law")

    merged = ET.tostring(root, encoding="unicode")
    _write_cache(cache_path, merged)
    return scrub(merged)


def confirm_zero(day: date) -> bool:
    """0건인 날을 한 번 재요청해 같은 결과인지 확인한다.

    `lsJoHstInf`의 실패 응답은 정상 0건과 바이트 단위로 동일하므로, 응답 형태만으로는
    구별할 수 없다. 독립적인 두 번째 요청이 같은 0을 주면 일시적 실패일 가능성이
    낮아진다. 이것으로 잡히지 않는 지속적 실패는 카나리아가 담당한다.

    반환값: 재요청도 정상 응답이고 0건이면 True.
    """
    stamp = day.strftime("%Y%m%d")
    response = _throttled_get(
        {"target": "lsJoHstInf", "type": "XML", "regDt": stamp, "display": str(PAGE_SIZE)}
    )
    if classify(response.text) is not ResponseKind.OK:
        return False
    root = ET.fromstring(response.text)
    return int(root.findtext("totalCnt") or 0) == 0


def probe_canary() -> int | None:
    """알려진 비-0 날짜를 호출해 세션이 정상인지 확인한다.

    카나리아 날짜(2025-04-01)는 0.7단계 픽스처에서 totalCnt=83으로 확인된 날이며
    측정 구간(2025-08-01~) 밖이라 집계를 오염시키지 않는다. 이 호출이 0이나 비정상을
    반환하면, 직전 카나리아 이후 기록한 0건들은 전부 신뢰할 수 없다.
    """
    response = _throttled_get(
        {"target": "lsJoHstInf", "type": "XML", "regDt": CANARY_DATE, "display": "1000"}
    )
    if classify(response.text) is not ResponseKind.OK:
        return None
    return int(ET.fromstring(response.text).findtext("totalCnt") or 0)


@dataclass
class DayResult:
    """하루치 수집 결과."""

    day: date
    kind: ResponseKind
    law_count: int = 0
    jo_count: int = 0
    total_cnt: int = 0
    truncated: bool = False
    zero_confirmed: bool | None = None


@dataclass
class LawStat:
    """법령 하나에 대한 12개월 집계."""

    law_id: str
    name: str = ""
    ministry: str = ""
    law_kind: str = ""
    amend_days: set[date] = field(default_factory=set)
    article_events: int = 0
    reasons: collections.Counter[str] = field(default_factory=collections.Counter)
    first_day: date | None = None
    last_day: date | None = None


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def parse_day(day: date, text: str) -> tuple[DayResult, list[tuple[str, str, str, str, str]]]:
    """캐시 본문을 파싱해 하루 결과와 (법령ID, 법령명, 부처, 구분, 변경사유) 행을 낸다."""
    kind = classify(text)
    if kind is not ResponseKind.OK:
        return DayResult(day=day, kind=kind), []

    root = ET.fromstring(text)
    total = int(root.findtext("totalCnt") or 0)
    laws = root.findall("law")
    rows: list[tuple[str, str, str, str, str]] = []
    jo_count = 0
    for law in laws:
        info = law.find("법령정보")
        if info is None:
            continue
        law_id = (info.findtext("법령ID") or "").strip()
        name = (info.findtext("법령명한글") or "").strip()
        ministry = (info.findtext("소관부처명") or "").strip()
        kind_name = (info.findtext("법령구분명") or "").strip()
        for jo in law.iter("jo"):
            jo_count += 1
            rows.append(
                (law_id, name, ministry, kind_name, (jo.findtext("변경사유") or "").strip())
            )

    return (
        DayResult(
            day=day,
            kind=kind,
            law_count=len(laws),
            jo_count=jo_count,
            total_cnt=total,
            truncated=len(laws) < total,
        ),
        rows,
    )


def _load_meta() -> dict[str, dict[str, object]]:
    if META_PATH.exists():
        loaded: dict[str, dict[str, object]] = json.loads(META_PATH.read_text(encoding="utf-8"))
        return loaded
    return {"zero_confirmed": {}, "canaries": {}}


def _save_meta(meta: dict[str, dict[str, object]]) -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def collect(
    start: date, end: date, *, fetch: bool = True, refresh: bool = False
) -> list[DayResult]:
    """구간 전체를 수집(또는 캐시 로드)하고 하루 단위 결과를 반환한다.

    0건인 날은 한 번 재요청해 확인하고, 30일마다 카나리아를 호출한다. 두 결과는
    `_meta.json`에 남겨 집계 단계가 네트워크 없이 읽을 수 있게 한다.
    """
    meta = _load_meta()
    zero_confirmed = meta.setdefault("zero_confirmed", {})
    canaries = meta.setdefault("canaries", {})

    results: list[DayResult] = []
    for index, day in enumerate(daterange(start, end)):
        stamp = day.strftime("%Y%m%d")
        cache_path = CACHE_DIR / f"{stamp}.xml"
        if cache_path.exists() and not refresh:
            text = cache_path.read_text(encoding="utf-8")
        elif fetch:
            if index % CANARY_EVERY_DAYS == 0:
                value = probe_canary()
                canaries[stamp] = value
                _save_meta(meta)
                if value is None or value < CANARY_EXPECTED_MIN:
                    raise RuntimeError(
                        f"카나리아 실패 ({stamp}, 값={value}). 세션이 깨졌다. "
                        "이 시점 이후의 0건은 신뢰할 수 없으므로 수집을 중단한다."
                    )
            text = fetch_day(day, refresh=refresh)
        else:
            continue

        result, _ = parse_day(day, text)
        if result.kind is ResponseKind.OK and result.total_cnt == 0:
            cached = zero_confirmed.get(stamp)
            if cached is not None and not refresh:
                result.zero_confirmed = bool(cached)
            elif fetch:
                result.zero_confirmed = confirm_zero(day)
                zero_confirmed[stamp] = result.zero_confirmed
                _save_meta(meta)
        results.append(result)
        print(
            f"{day} {result.kind.value:15} law={result.law_count:4d} jo={result.jo_count:5d}"
            f"{'  TRUNCATED' if result.truncated else ''}"
            f"{'' if result.zero_confirmed is None else f'  zero_ok={result.zero_confirmed}'}",
            file=sys.stderr,
        )
    _save_meta(meta)
    return results


def aggregate(
    start: date, end: date
) -> tuple[list[DayResult], dict[str, LawStat], collections.Counter[str]]:
    """캐시만 읽어 집계한다. 네트워크를 쓰지 않는다."""
    days: list[DayResult] = []
    stats: dict[str, LawStat] = {}
    reasons: collections.Counter[str] = collections.Counter()

    for day in daterange(start, end):
        cache_path = CACHE_DIR / f"{day.strftime('%Y%m%d')}.xml"
        if not cache_path.exists():
            continue
        result, rows = parse_day(day, cache_path.read_text(encoding="utf-8"))
        days.append(result)
        for law_id, name, ministry, kind_name, reason in rows:
            stat = stats.setdefault(law_id, LawStat(law_id=law_id))
            stat.name = stat.name or name
            stat.ministry = stat.ministry or ministry
            stat.law_kind = stat.law_kind or kind_name
            stat.amend_days.add(day)
            stat.article_events += 1
            stat.reasons[reason] += 1
            stat.first_day = day if stat.first_day is None else min(stat.first_day, day)
            stat.last_day = day if stat.last_day is None else max(stat.last_day, day)
            reasons[reason] += 1
    return days, stats, reasons


def write_csv(stats: dict[str, LawStat], out: Path) -> None:
    """법령별 집계를 CSV로 쓴다."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "법령ID",
                "법령명",
                "소관부처",
                "법령구분",
                "개정횟수",
                "조문건수",
                "최초개정일",
                "최종개정일",
            ]
        )
        for stat in sorted(stats.values(), key=lambda s: (-len(s.amend_days), -s.article_events)):
            writer.writerow(
                [
                    stat.law_id,
                    stat.name,
                    stat.ministry,
                    stat.law_kind,
                    len(stat.amend_days),
                    stat.article_events,
                    stat.first_day.isoformat() if stat.first_day else "",
                    stat.last_day.isoformat() if stat.last_day else "",
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--no-fetch", action="store_true", help="캐시만 사용")
    parser.add_argument("--refresh", action="store_true", help="캐시 무시하고 재수집")
    parser.add_argument("--csv", default=str(REPO_ROOT / "data" / "frequency-summary.csv"))
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    collect(start, end, fetch=not args.no_fetch, refresh=args.refresh)
    days, stats, reasons = aggregate(start, end)

    meta = _load_meta()
    zero_confirmed = meta.get("zero_confirmed", {})
    canaries = meta.get("canaries", {})

    ok = [d for d in days if d.kind is ResponseKind.OK]
    failed = [d for d in days if d.kind is not ResponseKind.OK]
    zero_days = [d for d in ok if d.total_cnt == 0]
    unconfirmed = [d for d in zero_days if not zero_confirmed.get(d.day.strftime("%Y%m%d"))]
    bad_canaries = {k: v for k, v in canaries.items() if v is None or int(v) < CANARY_EXPECTED_MIN}

    print(f"\n수집 성공 {len(ok)}일 / 실패 {len(failed)}일 / 구간 {len(daterange(start, end))}일")
    print(f"잘림 감지: {sum(1 for d in ok if d.truncated)}일")
    print(f"0건 {len(zero_days)}일 (재요청 미확인 {len(unconfirmed)}일)")
    print(f"카나리아 {len(canaries)}회, 실패 {len(bad_canaries)}회 {bad_canaries or ''}")
    print(f"법령 단위 이벤트 합계: {sum(len(s.amend_days) for s in stats.values())}")
    print(f"조문 단위 이벤트 합계: {sum(s.article_events for s in stats.values())}")
    print(f"변경사유 분포: {dict(reasons)}")

    write_csv(stats, Path(args.csv))
    print(f"CSV -> {args.csv}")


if __name__ == "__main__":
    main()
