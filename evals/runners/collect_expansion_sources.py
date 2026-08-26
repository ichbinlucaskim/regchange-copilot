"""골든셋 확장용 원문을 **ingest 경로 그대로** 수집한다 (6단계 §4).

    uv run python -m evals.runners.collect_expansion_sources --dry-run
    uv run python -m evals.runners.collect_expansion_sources

무엇을 하는가:
    12개월 캐시(`data/frequency-cache/`)에서 **아직 골든셋에 쓰지 않은 개정 이벤트
    12건**의 본문을, 그리고 가능하면 그 직전 버전 본문까지 받아 스냅샷으로 남긴다.

구현 이유:
    **탐색 스크립트(`scripts/analysis/collect_golden_sources.py`)를 쓰지 않는다.**
    그것은 0.7단계 API 실측용 일회성 도구이며 `params` 를 손으로 조립한다. 이 수집은
    골든셋의 원천이 되므로 **운영과 같은 경로를 지나야** 한다 —

      - `LawApiClient` : 호출 간격 1.2초, 재시도·백오프, 응답 형태 분류
      - `write_snapshot` : 매니페스트 + sha256 + **마스킹**(OC 유출 방지)
      - `resolve_previous_mst` : 직전 MST 를 `oldAndNew` 로 얻는다 (R-21 해소 경로)

    측정 코드가 운영 경로를 그대로 지나지 않으면 그 차이가 결과로 나타난다 —
    이 저장소가 두 번 겪은 일이다
    (`docs/incidents/measurement-reported-failure-as-success.md` §5-1).

    **`efYd` 소급 조회를 다시 시도하지 않는다.** 2026-08-19 에 6건 전부 실패했고
    (`docs/api-exploration/law-api-spec.md` §3.2), 확인된 실패를 반복하는 것은 공공 API
    에 대한 호출만 늘린다. 직전 버전은 `oldAndNew` 로 얻는다.

트레이드오프:
    - DB 에 적재하지 않는다. 골든셋 시나리오는 **본문 텍스트**만 필요하고, 적재하면
      `regulation_document` 에 평가용 행이 섞인다. 스냅샷은 남기므로 나중에
      `load_snapshot` 으로 적재할 수 있다.
    - 직전 버전이 없거나(제정본) `oldAndNew` 가 실패하면 그 MST 는 `before` 없이 남는다.
      골든셋 README §5 가 이미 그 상태를 `TODO(verify)` 로 다룬다.

엣지 케이스:
    - **이미 받은 MST**: `write_snapshot` 이 같은 디렉터리를 거부한다. 건너뛰고 기록한다 —
      중복 수집을 조용히 허용하면 어느 스냅샷이 정본인지 알 수 없다.
    - **직전 MST 가 이미 수집 대상에 있음**: 같은 이유로 건너뛴다.
    - 수집 실패: 그 MST 를 실패로 기록하고 **계속 진행한다.** 하나가 실패했다고 나머지를
      버리면 재수집이 처음부터가 된다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.config.settings import apply_dotenv, law_api_base_url, law_api_oc, snapshot_root
from regchange.ingest.client import CollectionFailure, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import LAW_DOCUMENT
from regchange.ingest.snapshot import (
    SnapshotExistsError,
    new_run_id,
    utc_now,
    write_snapshot,
)
from regchange.ingest.versions import resolve_previous_mst

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "frequency-cache"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

CORPUS_LAW_IDS = {
    "000030": "정보통신망 이용촉진 및 정보보호 등에 관한 법률",
    "004797": "정보통신망 이용촉진 및 정보보호 등에 관한 법률 시행령",
    "011357": "개인정보 보호법",
    "011468": "개인정보 보호법 시행령",
    "010199": "전자금융거래법",
    "010366": "전자금융거래법 시행령",
    "001540": "신용정보의 이용 및 보호에 관한 법률",
    "004105": "신용정보의 이용 및 보호에 관한 법률 시행령",
    "011359": "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법",
}
"""`config/corpus.yaml` 의 활성 법령 9종. 캐시에서 이 법령의 이벤트만 고른다."""

ALREADY_USED_MST = {
    "280277",
    "282481",
    "283503",
    "283839",
    "283841",
    "283843",
    "285199",
    "285955",
}
"""골든셋 15건이 이미 쓴 원천 8건 (`evals/datasets/golden/README.md` §1)."""

logger = logging.getLogger("collect")


def unused_events() -> list[dict[str, Any]]:
    """캐시에서 **아직 쓰지 않은** 개정 이벤트를 뽑는다.

    캐시는 `lsJoHstInf` 응답이며 **조문 목록만** 담는다 — 본문은 없다.
    그래서 이 함수가 고른 MST 로 본문을 다시 받아야 한다.
    """
    events: list[dict[str, Any]] = []
    for path in sorted(CACHE_DIR.glob("2*.xml")):
        try:
            root = ET.parse(path).getroot()  # noqa: S314 — 로컬 캐시
        except ET.ParseError:
            logger.warning("캐시 파싱 실패, 건너뛴다: %s", path)
            continue
        for law in root.findall("law"):
            info = law.find("법령정보")
            if info is None:
                continue
            law_id = (info.findtext("법령ID") or "").strip()
            mst = (info.findtext("법령일련번호") or "").strip()
            if law_id not in CORPUS_LAW_IDS or mst in ALREADY_USED_MST:
                continue
            articles = [
                (jo.findtext("조문번호") or "").strip() for jo in law.findall("조문정보/jo")
            ]
            events.append(
                {
                    "law_id": law_id,
                    "law_name": CORPUS_LAW_IDS[law_id],
                    "mst": mst,
                    "promulgation_date": info.findtext("공포일자"),
                    "promulgation_no": info.findtext("공포번호"),
                    "revision_kind": info.findtext("제개정구분명"),
                    "effective_date": info.findtext("시행일자"),
                    "article_count": len(articles),
                    "articles": articles,
                }
            )
    return sorted(events, key=lambda e: (e["revision_kind"], -e["article_count"]))


class BodyOutcome(StrEnum):
    """본문 수집 하나의 결과 — **셋이며 둘로 뭉치지 않는다**.

    `WRITTEN` 과 `ALREADY_HAVE` 는 **둘 다 본문을 확보한 상태**다. 하나로 세면
    확보량을 실제보다 적게 보고하게 되고, 2026-08-23 에 실제로 그랬다.
    """

    WRITTEN = "WRITTEN"
    """새로 받아 저장했다."""
    ALREADY_HAVE = "ALREADY_HAVE"
    """이미 같은 요청의 스냅샷이 있다. **확보된 상태이며 실패가 아니다.**"""
    FAILED = "FAILED"
    """받지 못했다. 이것만 실패다."""


@dataclass(frozen=True, slots=True)
class BodyResult:
    """결과와 위치. `directory` 는 `FAILED` 일 때만 `None` 이다."""

    outcome: BodyOutcome
    directory: str | None


async def collect_body(
    client: LawApiClient, store: Any, masker: Masker, mst: str, *, run_id: str, now: Any
) -> BodyResult:
    """본문 하나를 받아 스냅샷으로 쓴다.

    **반환값이 `str | None` 이 아니다.** `None` 은 "이미 있음"과 "실패"를 동시에
    뜻했고, 그 뭉갬이 확보량 오보고를 만들었다.
    """
    outcome = await client.collect(LAW_DOCUMENT, {"MST": mst})
    if isinstance(outcome, CollectionFailure):
        logger.error("본문 수집 실패 MST=%s: %s / %s", mst, outcome.reason.value, outcome.detail)
        return BodyResult(BodyOutcome.FAILED, None)
    try:
        manifest = await write_snapshot(
            store,
            outcome,
            run_id=run_id,
            fetched_at=now,
            params={"MST": mst},
            display=None,
            masker=masker,
        )
    except SnapshotExistsError as exc:
        logger.info("이미 확보돼 있다 MST=%s (%s)", mst, exc)
        return BodyResult(BodyOutcome.ALREADY_HAVE, None)
    return BodyResult(BodyOutcome.WRITTEN, manifest.directory)


async def run(*, dry_run: bool) -> None:
    apply_dotenv()
    events = unused_events()

    logger.info(
        "미사용 이벤트 %d건 / 조문 %d건", len(events), sum(e["article_count"] for e in events)
    )
    for e in events:
        logger.info(
            "  %-14s MST=%-8s %-6s 공포 %s 시행 %s 조문 %3d",
            e["law_name"][:14],
            e["mst"],
            e["revision_kind"],
            e["promulgation_date"],
            e["effective_date"],
            e["article_count"],
        )
    if dry_run:
        logger.info("--dry-run: 호출하지 않고 끝낸다")
        return

    oc = law_api_oc()
    masker = Masker(oc)
    store = LocalDocumentStore(snapshot_root())
    now = utc_now()
    run_id = new_run_id(now)
    rows: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as http:
        client = LawApiClient(law_api_base_url(), http, oc)
        for event in events:
            mst = str(event["mst"])
            after = await collect_body(client, store, masker, mst, run_id=run_id, now=now)

            previous_mst: str | None = None
            before = BodyResult(BodyOutcome.FAILED, None)
            try:
                previous = await resolve_previous_mst(
                    client, store, mst, run_id=run_id, fetched_at=now, masker=masker
                )
            except Exception:
                logger.exception("직전 버전 조회 실패 MST=%s", mst)
                previous = None
            if previous is not None:
                previous_mst = previous.previous.mst
                before = await collect_body(
                    client, store, masker, previous_mst, run_id=run_id, now=now
                )

            rows.append(
                {
                    **event,
                    "after_outcome": after.outcome.value,
                    "after_snapshot": after.directory,
                    "previous_mst": previous_mst,
                    "before_outcome": before.outcome.value,
                    "before_snapshot": before.directory,
                }
            )
            logger.info(
                "MST=%s 본문=%s 직전=%s (%s)",
                mst,
                after.outcome.value,
                previous_mst or "없음",
                before.outcome.value,
            )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"expansion-sources-{now:%Y%m%dT%H%M%SZ}.json"
    out.write_text(
        json.dumps({"run_id": run_id, "events": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # **확보와 신규 저장을 나눠 센다.** 하나로 세면 확보량이 실제보다 적게 보고된다.
    have = sum(1 for r in rows if r["after_outcome"] != BodyOutcome.FAILED)
    failed = [r["mst"] for r in rows if r["after_outcome"] == BodyOutcome.FAILED]
    before_have = sum(1 for r in rows if r["before_outcome"] != BodyOutcome.FAILED)
    logger.info(
        "본문 확보 %d/%d (실패 %s), 직전 본문 확보 %d건 → %s",
        have,
        len(rows),
        failed or "없음",
        before_have,
        out,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="골든셋 확장 원문 수집 (6단계 §4)")
    parser.add_argument("--dry-run", action="store_true", help="목록만 출력하고 호출하지 않는다")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
