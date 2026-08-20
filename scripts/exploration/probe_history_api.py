"""법령 연혁·신구법 API의 구조를 확인한다 (R-21 재탐색 전용).

목적:
    `target=lsHistory`(법령 연혁 목록)와 `target=oldAndNew`(신구법 목록/본문)를
    실제로 호출해 루트 태그·항목 요소·필드 구성을 확정하고 픽스처로 남긴다.
    R-21(이전 MST 확보 경로 미구현)이 닫히는지를 이 결과가 결정한다.

구현 이유:
    **`scripts/exploration/fetch.py`를 쓰지 않고 `LawApiClient`를 쓴다.** 그 모듈에는
    응답 형태 분류가 없다. 이번 탐색의 논점이 바로 **"미신청"이라는 응답 문구를
    액면 그대로 믿을 것인가**이므로, 문구가 아니라 `ResponseKind`로 형태를 분류하는
    경로로 호출해야 한다. 스로틀·마스킹·재시도도 함께 따라온다.

    **`TargetSpec`을 이 스크립트 안에 둔다. `src/`에 등록하지 않는다.** 아직 루트
    태그가 확정되지 않은 target을 운영 코드의 기준표(§2.1)에 넣으면, 추측이 스펙이
    된다. 이 저장소에서 그 표는 실측 결과만 담기로 했다. 구조가 확정된 뒤 등록하는
    것은 별도 작업이다.

트레이드오프:
    루트 태그를 모르는 채로 첫 호출을 보내므로 `ROOT_MISMATCH`가 날 수 있고, 그때
    호출을 한 번 더 쓴다(target당 최대 2회). 대신 **추측한 루트 태그를 확정으로
    기록하는 일이 구조적으로 불가능하다** — 확정값은 언제나 응답이 알려준 것이다.

    `ClassifiedFailure`는 본문을 발췌만 담으므로 첫 호출이 어긋나면 전문을 잃는다.
    그래서 어긋난 루트로 재호출해 전문을 받는다. 호출 1회를 더 쓰는 대신 픽스처가
    잘리지 않는다.

엣지 케이스:
    - `ROOT_MISMATCH` 응답의 `detail`에 실제 루트 태그가 들어 있다. 그 값을 뽑아
      spec을 고쳐 한 번만 재시도한다. 두 번은 하지 않는다 — 두 번 어긋나면 루트가
      응답마다 다르다는 뜻이고, 그것은 재시도가 아니라 기록할 사실이다.
    - `HTML`(미신청 안내)로 분류되면 **재시도하지 않는다.** 파라미터 문제가 아니며,
      그 사실 자체가 이번 탐색의 결과다. 본문을 `.html` 픽스처로 남긴다.
    - `LAW_MESSAGE`도 재시도하지 않는다. 같은 이유다.
    - 픽스처는 `Masker`를 통과한 문자열만 쓴다. 원본 바이트는 파일로 나가지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import replace
from pathlib import Path

import httpx

from regchange.ingest.client import LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import (
    ClassifiedOk,
    ResponseFamily,
    ResponseKind,
    TargetSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "law_api"

PROVISIONAL_ROOT = "LawSearch"
"""첫 시도에 쓰는 루트 태그. **추정값이며 확정이 아니다.**

lawSearch.do 계열이 지금까지 전부 `LawSearch`였으므로(§2.1) 출발점으로 쓴다.
어긋나면 응답이 실제 루트를 알려주고, 기록되는 것은 그 값이다.
"""

ROOT_IN_DETAIL = re.compile(r"루트 태그가 <([^>]+)>")
"""`ROOT_MISMATCH` detail에서 실제 루트 태그를 뽑는다 (`response.py` §분류)."""

# 가이드에서 읽은 값이다. 유추가 아니다.
#   법령 연혁 목록 조회 : lawSearch.do  target=lsHistory
#     https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=lsHstListGuide
#   신구법 목록 조회    : lawSearch.do  target=oldAndNew
#     https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=oldAndNewListGuide
#   신구법 본문 조회    : lawService.do target=oldAndNew  (ID 또는 MST 택1)
#     https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=oldAndNewInfoGuide

LS_HISTORY_LIST = TargetSpec(
    key="ls_history_list",
    target="lsHistory",
    endpoint="lawSearch.do",
    family=ResponseFamily.SEARCH,
    root_tag=PROVISIONAL_ROOT,
    item_path="law",
    required_params=frozenset({"query"}),
)

OLD_AND_NEW_LIST = TargetSpec(
    key="old_and_new_list",
    target="oldAndNew",
    endpoint="lawSearch.do",
    family=ResponseFamily.SEARCH,
    root_tag=PROVISIONAL_ROOT,
    item_path="oldAndNew",
    required_params=frozenset({"query"}),
)

OLD_AND_NEW_BODY = TargetSpec(
    key="old_and_new_body",
    target="oldAndNew",
    endpoint="lawService.do",
    family=ResponseFamily.DOCUMENT,
    root_tag=PROVISIONAL_ROOT,
    item_path="*",
    required_params=frozenset({"MST"}),
)

ADMRUL_OLD_AND_NEW_LIST = TargetSpec(
    key="admrul_old_and_new_list",
    target="admrulOldAndNew",
    endpoint="lawSearch.do",
    family=ResponseFamily.SEARCH,
    root_tag=PROVISIONAL_ROOT,
    item_path="admrulOldAndNew",
    required_params=frozenset({"query"}),
)

ADMRUL_OLD_AND_NEW_BODY = TargetSpec(
    key="admrul_old_and_new_body",
    target="admrulOldAndNew",
    endpoint="lawService.do",
    family=ResponseFamily.DOCUMENT,
    root_tag=PROVISIONAL_ROOT,
    item_path="*",
    required_params=frozenset({"ID"}),
)

INFOSEC_LAW = "정보통신망 이용촉진 및 정보보호 등에 관한 법률"
"""탐색 대상. 코퍼스 9종 중 12개월 개정일이 가장 많다(4회, `amendment-frequency.md` B-2).
버전이 여럿이어야 "직전 버전 특정"이 실제로 시험된다."""

PROBES: tuple[tuple[str, TargetSpec, dict[str, str]], ...] = (
    ("lshistory_000030", LS_HISTORY_LIST, {"query": INFOSEC_LAW, "display": "100"}),
    ("oldandnew_search_000030", OLD_AND_NEW_LIST, {"query": INFOSEC_LAW, "display": "100"}),
    ("oldandnew_000030_mst285199", OLD_AND_NEW_BODY, {"MST": "285199"}),
    # R-21의 결정적 확인 — "구조문이 직전 버전인가"를 두 번째 표본으로 검증한다.
    # 285199의 구조문이 283843이었으므로, 283843의 구조문은 282481이어야 한다
    # (정보통신망법 2026 공포 순서: 282481 → 283843 → 285199).
    # 한 표본으로는 "직전"인지 "임의의 이전 버전"인지 구별되지 않는다.
    ("oldandnew_000030_mst283843", OLD_AND_NEW_BODY, {"MST": "283843"}),
    # ADR-006 재검토용. 행정규칙은 조문 식별자를 하나도 주지 않는데(§6.1),
    # 법령 쪽 신구법이 대비표를 준다는 것이 확인됐으므로 행정규칙 쪽도 같은지 본다.
    # 대상: 전자금융감독규정 — 코퍼스의 행정규칙 4종 중 3단계 착수 1순위(ADR-006).
    ("admrul_oldandnew_search_efsv", ADMRUL_OLD_AND_NEW_LIST, {"query": "전자금융감독규정"}),
    # 위 목록의 1번(전자금융감독규정, 발령 20260715 일부개정)의 신구법일련번호.
    # `ID`는 행정규칙**일련번호**다 — 행정규칙ID(21828)를 넣으면 실패한다(§3.6).
    ("admrul_oldandnew_efsv_2100000282622", ADMRUL_OLD_AND_NEW_BODY, {"ID": "2100000282622"}),
    # 연쇄 확인 — 위 응답의 구조문 일련번호(2100000274812, 발령 20260213)로 다시 호출한다.
    # 목록 조회는 현행 1건만 주므로, 과거로 거슬러 가려면 이 연쇄가 성립해야 한다.
    # 비-현행 ID에도 본문 조회가 동작하는지가 함께 시험된다.
    ("admrul_oldandnew_efsv_2100000274812", ADMRUL_OLD_AND_NEW_BODY, {"ID": "2100000274812"}),
    # 제정본은 직전 버전이 없다. 그때 응답이 어떤 형태인지가 `resolve_previous_mst` 의
    # None 판정 근거가 된다 — 구조문_기본정보가 아예 없는지, 있는데 비어 있는지,
    # 아니면 다른 형태인지. 추측하지 않고 실측한다.
    # 대상: 지방보조금통합관리망의 관리 및 운영에 관한 규칙 (제정, 공포 20260731).
    # 캐시에서 제개정구분명=제정 인 이벤트 159건 중 가장 최근 것을 골랐다.
    ("oldandnew_288527_enacted", OLD_AND_NEW_BODY, {"MST": "288527"}),
)


def load_env() -> dict[str, str]:
    """`.env`를 읽어 키-값으로 돌려준다."""
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.split("#")[0].strip()
    return env


def base_url_of(env: dict[str, str]) -> str:
    """`.env`의 전체 URL에서 endpoint 앞까지를 잘라낸다."""
    configured = env.get("LAW_GO_KR_BASE_URL", "").strip()
    if configured.endswith(".do"):
        return configured.rsplit("/", 1)[0]
    return configured.rstrip("/")


def save(name: str, body: str, masker: Masker, *, suffix: str = "xml") -> Path:
    """픽스처를 저장한다. 마스킹을 통과하지 못하면 저장하지 않는다."""
    masked = masker.mask(body)
    path = FIXTURE_DIR / f"{name}.{suffix}"
    path.write_text(masked, encoding="utf-8")
    return path


async def probe(
    client: LawApiClient,
    masker: Masker,
    name: str,
    spec: TargetSpec,
    params: dict[str, str],
) -> None:
    """한 target을 호출하고 결과를 보고한다. 루트 어긋남은 한 번만 재시도한다."""
    print(f"\n=== {name} — {spec.endpoint}?target={spec.target} {params} ===")

    result = await client.fetch(spec, params)

    if not isinstance(result, ClassifiedOk):
        kind = getattr(result, "kind", None)
        detail = getattr(result, "detail", "")
        excerpt = getattr(result, "body_excerpt", "")
        print(f"  분류: {kind}")
        print(f"  detail: {detail}")
        print(f"  발췌: {excerpt[:300]}")

        matched = ROOT_IN_DETAIL.search(detail) if kind is ResponseKind.ROOT_MISMATCH else None
        if matched is None:
            suffix = "html" if kind is ResponseKind.HTML else "xml"
            path = save(f"error_{name}", excerpt, masker, suffix=suffix)
            print(f"  → 재시도하지 않는다. 발췌 저장: {path.name}")
            return

        actual = matched.group(1)
        print(f"  → 실제 루트 <{actual}>. 그 값으로 한 번만 재호출한다")
        result = await client.fetch(replace(spec, root_tag=actual), params)
        if not isinstance(result, ClassifiedOk):
            again = getattr(result, "kind", None)
            print(f"  재호출도 실패: {again} / {getattr(result, 'detail', '')}")
            return

    path = save(name, result.body, masker)
    children = [c.tag for c in result.root][:12]
    print(f"  분류: OK  루트=<{result.root.tag}>  totalCnt={result.total_count}")
    print(f"  항목({spec.item_path}) {len(result.items)}건  자식 태그: {children}")
    print(f"  저장: {path.name} ({len(result.body):,}자)")


async def main() -> None:
    """선택된 probe를 순서대로 호출한다. 클라이언트는 하나만 만든다.

    `--only`로 좁힐 수 있게 한 이유: 이미 픽스처를 받은 probe를 다시 부르면 공공
    API에 무의미한 부하를 준다. 픽스처가 있으면 건너뛰는 방식도 있으나, 그러면
    "다시 확인하고 싶을 때" 지우는 절차가 필요해진다 — 선택이 더 단순하다.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=None, help="실행할 probe 이름")
    args = parser.parse_args()

    selected = [p for p in PROBES if args.only is None or p[0] in set(args.only)]
    if not selected:
        raise SystemExit(f"해당하는 probe가 없다. 가능한 값: {[p[0] for p in PROBES]}")

    env = load_env()
    masker = Masker(env["LAW_GO_KR_OC"])
    async with httpx.AsyncClient() as http:
        client = LawApiClient(base_url_of(env), http, env["LAW_GO_KR_OC"])
        for name, spec, params in selected:
            await probe(client, masker, name, spec, params)


if __name__ == "__main__":
    asyncio.run(main())
