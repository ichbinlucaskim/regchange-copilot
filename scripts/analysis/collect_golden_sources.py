"""골든셋 시나리오 설계에 쓸 조문 원문을 수집한다 (2-A단계 전용).

목적:
    `data/frequency-cache/`에서 확정된 개정 8건에 대해, 개정 후 법령 본문과
    (필요한 경우) 개정 직전 본문을 법제처에서 받아 스냅샷으로 보관한다. 시나리오
    YAML의 `before`/`after` 필드가 이 스냅샷을 근거로 채워진다.

구현 이유:
    `scripts/exploration/fetch.py`를 쓰지 않는다. 그 모듈은 0.7단계 구조 파악용
    이며 스로틀 외에 응답 형태 분류·무결성 검사·매니페스트가 없다. **골든셋의
    `before`/`after`는 나중에 "이 텍스트가 정말 법제처가 준 것인가"를 감사에서
    되물을 대상**이므로, 원문성이 증명되는 경로로 받아야 한다. 그래서
    `LawApiClient` + `write_snapshot`을 그대로 쓴다 — 마스킹·sha256·매니페스트가
    따라온다.

    개정 후는 `LAW_DOCUMENT`(MST만)로, 개정 전은 `EFLAW_DOCUMENT`(MST + 시행일
    하루 전 efYd)로 받는다. 후자를 나눈 이유는 **개정 전 버전의 MST를 우리가
    모르기 때문**이다. 연혁 목록(`lsHstInf`)을 먼저 받아 이전 MST를 찾는 방법도
    있으나 그쪽 `regDt`는 공포일자가 아니어서(ADR-005) 버전 선택 기준이 흐리고,
    호출도 두 배가 된다. `efYd` 소급 조회로 한 번에 끝나면 그쪽이 낫다.

트레이드오프:
    **`efYd` 소급 조회의 동작은 미확인이다** (`law-api-spec.md` §8). 같은 MST에
    서로 다른 `efYd`가 서로 다른 응답을 낸다는 실측(`eflaw_009256_mst261379_*`
    3종)은 있으나, 그 `efYd`들이 전부 해당 MST의 시행일 이후였다. 시행일 **이전**
    날짜를 주면 무엇이 오는지는 관측된 바 없다. 실패를 예상 범위로 두고, 실패한
    조문은 `before`를 미확인으로 남긴 채 진행한다 — 시나리오는 `after`와
    변경사유만으로도 성립한다.

    법령 단위로 받는다. 조문 단위(`JO`)로 좁히면 호출이 조문 수만큼 늘고, 응답
    하나가 그 법령의 전 조문을 담으므로 좁힐 이유가 없다. 대신 스냅샷 용량이
    커진다(최대 실측 2.1MB).

엣지 케이스:
    - 응답 형태 실패(`LAW_MESSAGE`/`HTML`/`EMPTY_BODY`)는 재시도하지 않는다.
      `LawApiClient`가 이미 그렇게 동작하며, 여기서는 실패 사유를 기록만 한다.
    - 같은 요청의 스냅샷이 이미 있으면 **호출하지 않고 그것을 재사용한다.**
      `write_snapshot`이 덮어쓰기를 거부하므로 재실행이 실패로 끝나는데, 그러면
      한쪽만 다시 돌릴 수가 없어 결국 전부를 다시 때리게 된다. 재실행이 무료여야
      부분 재수집이 가능하다(`amendment_frequency.py`의 캐시와 같은 발상).
    - 이미 픽스처로 확보된 버전(전금법 MST=280277, 개보법 MST=270351)은 목록에서
      제외한다. 같은 것을 다시 받는 호출은 공공 API에 대한 낭비다.
    - 실패는 리포트의 `failed`에 **응답 형태(`response_kind`)와 함께** 남는다.
      사유 없이 건수만 남기면 나중에 `oldAndNew` 탐색이 필요한지 판단할 근거가
      없다. 한쪽만 재실행해도 다른 쪽 기록이 지워지지 않는다(`merge_records`).
    - OC는 `Masker`가 저장 직전에 마스킹한다. 이 스크립트는 본문 문자열을 직접
      파일에 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.ingest.client import Collection, CollectionFailure, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import EFLAW_DOCUMENT, LAW_DOCUMENT, TargetSpec
from regchange.ingest.snapshot import (
    MANIFEST_FILENAME,
    Manifest,
    encode_key,
    new_run_id,
    semantic_params,
    utc_now,
    write_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = REPO_ROOT / "data" / "snapshots"
REPORT_PATH = REPO_ROOT / "data" / "golden-sources-collection.json"


@dataclass(frozen=True, slots=True)
class Fetch:
    """수집 대상 한 건. 개정 하나의 before 또는 after다.

    목적:
        "어느 개정의 어느 쪽인가"와 "어떤 요청을 보내는가"를 한 값에 묶는다.

    구현 이유:
        요청 파라미터만 나열하면 나중에 스냅샷 디렉터리를 보고 그것이 어느
        시나리오의 before인지 알 수 없다. `case`와 `side`를 값에 넣어 수집
        리포트가 시나리오 설계로 바로 이어지게 했다.

    트레이드오프:
        `spec`과 `params`가 서로 맞물린 값인데 타입으로 강제되지 않는다
        (`EFLAW_DOCUMENT`에 `efYd`가 없으면 요청 전에 걸린다). 스펙별 하위 타입을
        만드는 대신 `LawApiClient.missing_params`의 사전 검증에 맡겼다 — 검증이
        이미 한 곳에 있는데 타입으로 또 만들면 두 벌이 된다.

    엣지 케이스:
        - `side`가 `"before"`인데 `spec`이 `LAW_DOCUMENT`인 조합은 만들지 않는다.
          개정 전은 시행일 기준 조회여야 하므로 `EFLAW_DOCUMENT`만 쓴다.
    """

    case: str
    law_id: str
    law_name: str
    side: str
    spec: TargetSpec
    params: dict[str, str]
    note: str


# 개정 후 본문. `제개정구분명`·공포번호·시행일은 `data/frequency-cache/` 실측값이다.
AFTER_FETCHES: tuple[Fetch, ...] = (
    Fetch(
        case="amend-04",
        law_id="000030",
        law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
        side="after",
        spec=LAW_DOCUMENT,
        params={"MST": "282481"},
        note="공포 20260106 제21305호 일부개정 / 시행 20260707 / 조문 26 (본조신설 16)",
    ),
    Fetch(
        case="amend-05",
        law_id="000030",
        law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
        side="after",
        spec=LAW_DOCUMENT,
        params={"MST": "283843"},
        note="공포 20260310 제21445호 타법개정 / 시행 20260911 / 제45조의3 1건",
    ),
    Fetch(
        case="amend-06",
        law_id="000030",
        law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
        side="after",
        spec=LAW_DOCUMENT,
        params={"MST": "285199"},
        note="공포 20260331 제21500호 일부개정 / 시행 20261001 / 조문 26",
    ),
    Fetch(
        case="amend-11",
        law_id="011357",
        law_name="개인정보 보호법",
        side="after",
        spec=LAW_DOCUMENT,
        params={"MST": "283839"},
        note="공포 20260310 제21445호 일부개정 / 시행 20260911 / 조문 19",
    ),
    Fetch(
        case="amend-14",
        law_id="011468",
        law_name="개인정보 보호법 시행령",
        side="after",
        spec=LAW_DOCUMENT,
        params={"MST": "283503"},
        note="공포 20260219 제36121호 일부개정 / 시행 20260820 / 조문 9",
    ),
    Fetch(
        case="amend-16",
        law_id="001540",
        law_name="신용정보의 이용 및 보호에 관한 법률",
        side="after",
        spec=LAW_DOCUMENT,
        params={"MST": "283841"},
        note="공포 20260310 제21445호 타법개정 / 시행 20260911 / 제20조 1건",
    ),
    Fetch(
        case="amend-17",
        law_id="001540",
        law_name="신용정보의 이용 및 보호에 관한 법률",
        side="after",
        spec=LAW_DOCUMENT,
        params={"MST": "285955"},
        note="공포 20260512 제21646호 일부개정 / 시행 20260813 / 제44조의2~5 전부 본조신설",
    ),
)

# 개정 전 본문. efYd 는 각 개정의 **시행일 하루 전**이다.
#
# `amend-11`은 개정 전 픽스처(`law_011357_mst270351_privacy.xml`, 시행 20251002)를
# 이미 갖고 있다. 그런데도 한 건 받는 이유는 **소급 조회 방식 자체의 대조군**이
# 필요하기 때문이다 — 같은 법령의 개정 전 텍스트를 두 경로로 얻어 일치하는지 보면,
# 나머지 5건의 소급 조회 결과를 신뢰할 근거가 생긴다. 대조군 없이 미확인 방식의
# 결과만 5건 쌓으면 그 5건이 맞는지 알 방법이 없다.
BEFORE_FETCHES: tuple[Fetch, ...] = (
    Fetch(
        case="amend-01",
        law_id="010199",
        law_name="전자금융거래법",
        side="before",
        spec=EFLAW_DOCUMENT,
        params={"MST": "280277", "efYd": "20261216"},
        note="시행 20261217 직전. after 는 픽스처 law_010199_mst280277.xml",
    ),
    Fetch(
        case="amend-04",
        law_id="000030",
        law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
        side="before",
        spec=EFLAW_DOCUMENT,
        params={"MST": "282481", "efYd": "20260706"},
        note="시행 20260707 직전",
    ),
    Fetch(
        case="amend-06",
        law_id="000030",
        law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
        side="before",
        spec=EFLAW_DOCUMENT,
        params={"MST": "285199", "efYd": "20260930"},
        note="시행 20261001 직전. amend-05(시행 20260911) 반영 상태여야 한다 — 대조 가능",
    ),
    Fetch(
        case="amend-11",
        law_id="011357",
        law_name="개인정보 보호법",
        side="before",
        spec=EFLAW_DOCUMENT,
        params={"MST": "283839", "efYd": "20260910"},
        note="시행 20260911 직전. 픽스처(MST=270351, 시행 20251002)와 대조하는 대조군",
    ),
    Fetch(
        case="amend-14",
        law_id="011468",
        law_name="개인정보 보호법 시행령",
        side="before",
        spec=EFLAW_DOCUMENT,
        params={"MST": "283503", "efYd": "20260819"},
        note="시행 20260820 직전",
    ),
    Fetch(
        case="amend-17",
        law_id="001540",
        law_name="신용정보의 이용 및 보호에 관한 법률",
        side="before",
        spec=EFLAW_DOCUMENT,
        params={"MST": "285955", "efYd": "20260812"},
        note="시행 20260813 직전",
    ),
)


def load_env() -> dict[str, str]:
    """`.env`를 읽어 키-값으로 돌려준다. 주석과 빈 줄은 건너뛴다."""
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.split("#")[0].strip()
    return env


def base_url_of(env: dict[str, str]) -> str:
    """`.env`의 전체 URL에서 endpoint 앞까지를 잘라낸다.

    `LAW_GO_KR_BASE_URL`은 `lawService.do`까지 포함한 값으로 설정돼 있는 반면
    `LawApiClient`는 endpoint를 직접 붙이므로, 마지막 경로 조각을 떼어낸다.
    `.do`로 끝나지 않으면 이미 디렉터리이므로 그대로 쓴다.
    """
    configured = env.get("LAW_GO_KR_BASE_URL", "").strip()
    if configured.endswith(".do"):
        return configured.rsplit("/", 1)[0]
    return configured.rstrip("/")


def find_existing(item: Fetch) -> Manifest | None:
    """같은 요청의 스냅샷이 이미 있으면 그 매니페스트를 돌려준다.

    목적:
        재실행 시 이미 받은 것을 다시 받지 않는다.

    구현 이유:
        `run_id`를 모르므로 디렉터리를 전부 뒤져야 할 것 같지만 그렇지 않다.
        스냅샷 경로는 `{run_id}/{target}/{encode_key(spec, params)}`이고 뒤의 두
        조각은 요청에서 계산된다. 그래서 `*/target/key/manifest.json` 한 번의
        glob으로 끝난다 — **키 인코딩이 곧 색인**이다.

    트레이드오프:
        `encode_key`는 6자리 해시 절단이므로 이론상 다른 요청과 충돌할 수 있다.
        그래서 매니페스트의 `params`를 다시 대조한다. 대조 없이 경로만 믿으면
        **다른 요청의 응답을 이 요청의 결과로 쓰게 되고, 그 오류는 조용하다.**

    엣지 케이스:
        - 매니페스트가 여러 run_id에 존재하면 사전순 마지막(= 가장 최근 실행)을
          쓴다. `new_run_id`가 시각 접두라 사전순이 시간순이다.
        - `params`가 다른 매니페스트가 잡히면 없는 것으로 본다. 해시 충돌이며,
          이때 `write_snapshot`이 같은 디렉터리를 거부해 실패로 드러난다.
    """
    expected = semantic_params(item.spec, item.params)
    pattern = f"*/{item.spec.target}/{encode_key(item.spec, item.params)}/{MANIFEST_FILENAME}"
    for path in sorted(SNAPSHOT_ROOT.glob(pattern), reverse=True):
        manifest = Manifest.from_json(path.read_text(encoding="utf-8"))
        if dict(manifest.params) == expected:
            return manifest
    return None


async def collect_one(
    client: LawApiClient,
    store: LocalDocumentStore,
    masker: Masker,
    item: Fetch,
    *,
    run_id: str,
    fetched_at: datetime,
) -> dict[str, object]:
    """한 건을 받아 스냅샷으로 쓰고 결과 레코드를 돌려준다.

    목적:
        성공이든 실패든 **같은 형태의 레코드**를 남긴다.

    구현 이유:
        실패를 예외로 올리지 않는다. `efYd` 소급 조회 실패는 예상 범위이고,
        한 건이 실패했다고 나머지 12건 수집을 중단하면 공공 API를 다시 때려야
        한다. 대신 실패 사유(`reason`)와 **응답 형태**(`response_kind`)를 반드시
        기록한다 — 나중에 `oldAndNew` 탐색이 필요해질 때 그 기록이 근거가 된다.

    트레이드오프:
        호출부가 레코드의 `ok`를 확인해야 한다. 실패를 조용히 넘기는 것처럼 보이지만,
        리포트 파일에 남고 표준출력에도 찍히므로 조용하지 않다.

    엣지 케이스:
        - 요청 전 파라미터 검증 실패(`BAD_REQUEST`)도 같은 형태로 기록된다.
          네트워크를 쓰지 않았다는 사실은 `request_count=0`으로 드러난다.
        - 성공했는데 조문이 0개인 응답은 여기서 걸러내지 않는다. 본문 계열은
          완주 검사 대상이 아니며, 내용 판단은 수집의 책임이 아니다.
    """
    record: dict[str, object] = {
        "case": item.case,
        "side": item.side,
        "law_id": item.law_id,
        "law_name": item.law_name,
        "target": item.spec.target,
        "params": dict(item.params),
        "note": item.note,
    }

    cached = find_existing(item)
    if cached is not None:
        record |= {
            "ok": True,
            "directory": cached.directory,
            "pages": len(cached.pages),
            "items": cached.received,
            "reused": True,
        }
        return record

    outcome = await client.collect(item.spec, item.params)
    if isinstance(outcome, CollectionFailure):
        record |= {
            "ok": False,
            "reason": outcome.reason.value,
            "response_kind": None if outcome.response_kind is None else outcome.response_kind.value,
            "detail": outcome.detail,
            "request_count": outcome.stats.requests,
        }
        return record

    collection: Collection = outcome
    manifest = await write_snapshot(
        store,
        collection,
        run_id=run_id,
        fetched_at=fetched_at,
        params=item.params,
        display=None,
        masker=masker,
    )
    record |= {
        "ok": True,
        "directory": manifest.directory,
        "pages": len(manifest.pages),
        "articles": len(collection.articles),
        "items": manifest.received,
        "request_count": collection.stats.requests,
        "reused": False,
    }
    return record


async def run(selected: tuple[Fetch, ...], *, run_id: str) -> list[dict[str, object]]:
    """선정된 대상을 순서대로 수집한다. 클라이언트는 하나만 만든다.

    `LawApiClient`가 스로틀 상태를 인스턴스에 들고 있으므로, 인스턴스를 여럿
    만들면 1.2초 간격이 깨진다(클라이언트 docstring의 트레이드오프 절).
    """
    env = load_env()
    store = LocalDocumentStore(SNAPSHOT_ROOT)
    masker = Masker(env["LAW_GO_KR_OC"])
    fetched_at = utc_now()

    records: list[dict[str, object]] = []
    async with httpx.AsyncClient() as http:
        client = LawApiClient(base_url_of(env), http, env["LAW_GO_KR_OC"])
        for item in selected:
            record = await collect_one(
                client, store, masker, item, run_id=run_id, fetched_at=fetched_at
            )
            status = ("REUSE" if record.get("reused") else "OK   ") if record["ok"] else "FAIL "
            print(f"[{status}] {item.case}/{item.side} {item.law_name} {item.params}")
            if not record["ok"]:
                print(f"       reason={record['reason']} kind={record['response_kind']}")
            records.append(record)
    return records


def load_previous_records() -> list[dict[str, object]]:
    """직전 리포트의 레코드를 읽는다. 없으면 빈 목록이다."""
    if not REPORT_PATH.exists():
        return []
    previous: list[dict[str, object]] = json.loads(REPORT_PATH.read_text(encoding="utf-8"))[
        "records"
    ]
    return previous


def merge_records(
    previous: list[dict[str, object]], current: list[dict[str, object]]
) -> list[dict[str, object]]:
    """`(case, side)`를 키로 이전 레코드 위에 이번 결과를 덮는다.

    목적:
        `--side after`만 돌려도 리포트에 before 결과가 남아 있게 한다.

    구현 이유:
        한쪽만 다시 돌리는 것이 정상 사용법인데(before는 실패하고 after는 이미
        받았다), 매번 통째로 덮어쓰면 **돌리지 않은 쪽이 리포트에서 사라진다.**
        사라진 것과 시도하지 않은 것이 구별되지 않으면 "before 6건 실패"라는
        사실 자체를 잃는다.

    트레이드오프:
        오래된 실패 기록이 계속 남는다. 그것이 의도다 — 실패는 지워지는 것이
        아니라 갱신되는 것이며, 성공으로 바뀌면 같은 키가 덮인다.

    엣지 케이스:
        - 이전에 없던 키는 그대로 추가된다.
        - 정렬은 `(case, side)` 사전순이다. 실행 순서가 아니라 대상 순서로 읽는다.
    """
    merged = {(str(r["case"]), str(r["side"])): r for r in previous}
    merged |= {(str(r["case"]), str(r["side"])): r for r in current}
    return [merged[key] for key in sorted(merged)]


def main() -> None:
    """수집을 실행하고 리포트를 쓴다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--side",
        choices=("after", "before", "all"),
        default="all",
        help="수집할 쪽. before 는 efYd 소급 조회이며 실패할 수 있다",
    )
    parser.add_argument("--run-id", default=None, help="스냅샷 run_id. 생략하면 새로 만든다")
    args = parser.parse_args()

    pool = {
        "after": AFTER_FETCHES,
        "before": BEFORE_FETCHES,
        "all": AFTER_FETCHES + BEFORE_FETCHES,
    }[args.side]

    run_id = args.run_id or new_run_id(datetime.now(UTC))
    records = merge_records(load_previous_records(), asyncio.run(run(pool, run_id=run_id)))

    report = {
        "run_id": run_id,
        "collected_at": utc_now().isoformat(),
        "requested": len(records),
        "succeeded": sum(1 for r in records if r["ok"]),
        "failed": [r for r in records if not r["ok"]],
        "records": records,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n리포트: {REPORT_PATH.relative_to(REPO_ROOT)}")
    print(f"성공 {report['succeeded']} / 요청 {report['requested']}")


if __name__ == "__main__":
    main()
