"""골든셋 `source.after` 가 조문 전문의 몇 %인지 재고 케이스 파일에 기록한다.

    uv run python scripts/analysis/after_coverage.py            # 측정만 (미리보기)
    uv run python scripts/analysis/after_coverage.py --write    # 케이스 YAML 갱신

목적:
    각 케이스의 `source.after`(= 신구법 대비표의 **개정된 항**)를 같은 조문의
    `assemble_body()` 전문과 대조해 길이 비율을 낸다. 그 값이 케이스 파일의
    `after_coverage` 이며, `tests/unit/test_golden_dataset.py` 가 하한을 검사한다.

구현 이유:
    **`assemble_body()` 를 부른다. 파서 중간값(`조문내용`)을 직접 읽지 않는다.**
    운영 경로가 쓰는 함수를 그대로 지나야 "운영이 보는 전문"과 대조한 것이 된다 —
    일회성 조사 코드가 그 경로를 우회해 없는 발견을 만든 사례가 있다
    (`docs/incidents/measurement-reported-failure-as-success.md` §5-1 다섯 번째).

    비율을 **케이스 파일에 기록**하고 테스트는 기록된 값을 검사한다. 테스트가 직접
    스냅샷을 읽지 않는 이유는 `data/snapshots/` 를 커밋하지 않기 때문이다 —
    스냅샷 없는 환경에서 테스트가 조용히 통과하면 그 통과는 아무것도 뜻하지 않는다.
    대신 이 스크립트가 재측정으로 기록값의 표류를 잡는다(`--write` 없이 돌리면 차이만
    출력한다).

트레이드오프:
    같은 사실이 두 곳(스냅샷, 케이스 파일)에 있게 된다. 그 대가로 **스냅샷 없이도
    하한이 강제된다.** 표류를 막는 것은 이 스크립트를 다시 돌리는 일이며,
    돌리지 않으면 기록값이 낡는다 — 그래서 `measured_at` 을 함께 적는다.

    문자 수로 잰다. 토큰 수가 검색·모델 입력에 더 가깝지만 토크나이저에 의존하게 되고,
    임베딩 모델을 바꾸면 과거 측정과 비교할 수 없게 된다.

엣지 케이스:
    - 스냅샷이 없는 원천(MST=280277): `ratio: null`, `reason: NO_SNAPSHOT`.
      0.0 으로 채우면 "덮이지 않았다"와 "잴 수 없었다"가 같은 값이 된다
    - `article_path` 가 조문 하나를 가리키지 않음(`제44조의11~26`, `제2조·제13조의2`,
      `제25조의2 외`): `ratio: null`, `reason: MULTI_ARTICLE`. 여러 조문의 전문을
      합쳐 분모로 쓰면 비율이 조문 수에 따라 달라져 하한이 의미를 잃는다
    - 조문이 스냅샷에 없음: `reason: ARTICLE_NOT_FOUND`. 조용히 건너뛰지 않는다 —
      케이스가 존재하지 않는 조문을 가리키고 있다는 뜻이다
    - 전문이 빈 문자열: 분모가 0이므로 `reason: EMPTY_ASSEMBLY`. 나눗셈을 하지 않는다
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Any

import yaml

from regchange.config.settings import snapshot_root
from regchange.parse import assemble_body, parse_law_document

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"

SINGLE_ARTICLE = re.compile(r"^제(\d+)조(?:의(\d+))?$")
"""`article_path` 가 **조문 하나**를 가리키는 형태. 이 형태가 아니면 측정하지 않는다."""


def find_snapshot(mst: str) -> list[Path]:
    """해당 MST 의 가장 최근 `law` 스냅샷 페이지 파일들. 없으면 빈 목록."""
    dirs = sorted(snapshot_root().glob(f"*/law/MST-{mst}-*"))
    return sorted(dirs[-1].glob("page-*.xml")) if dirs else []


def measure(case: dict[str, Any]) -> dict[str, Any]:
    """케이스 하나의 `after_coverage` 를 계산한다.

    반환값의 `ratio` 가 None 이면 `reason` 이 왜인지를 말한다. 둘을 함께 봐야
    "덮임이 낮다"와 "잴 수 없다"가 구별된다.
    """
    source = case["source"]
    path = str(source.get("article_path") or "")
    after = str(source.get("after") or "")

    match = SINGLE_ARTICLE.match(path.strip())
    if match is None:
        return {"ratio": None, "reason": "MULTI_ARTICLE", "article_path": path}

    pages = find_snapshot(str(source["mst"]))
    if not pages:
        return {"ratio": None, "reason": "NO_SNAPSHOT", "article_path": path}

    article_no = int(match.group(1))
    branch_no = int(match.group(2) or 0)
    assembled: str | None = None
    for page in pages:
        for unit in parse_law_document(page).units:
            if unit.article_no == article_no and unit.branch_no == branch_no:
                assembled = assemble_body(unit).raw

    if assembled is None:
        return {"ratio": None, "reason": "ARTICLE_NOT_FOUND", "article_path": path}
    if not assembled:
        return {"ratio": None, "reason": "EMPTY_ASSEMBLY", "article_path": path}

    return {
        "ratio": round(len(after) / len(assembled), 4),
        "after_chars": len(after),
        "assembled_chars": len(assembled),
        "measured_at": dt.datetime.now(tz=dt.UTC).date().isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="골든셋 after_coverage 측정")
    parser.add_argument("--write", action="store_true", help="케이스 YAML 에 기록한다")
    args = parser.parse_args()

    for path in sorted(GOLDEN_DIR.glob("case-*.yaml")):
        text = path.read_text(encoding="utf-8")
        case = yaml.safe_load(text)
        got = measure(case)
        old = case.get("after_coverage") or {}
        mark = "" if old.get("ratio") == got.get("ratio") else "  <-- 변경"
        print(
            f"{case['id']}  {got.get('ratio')}  {got.get('reason', '')}"
            f"  ({got.get('after_chars', '-')}/{got.get('assembled_chars', '-')}){mark}"
        )
        if not args.write:
            continue

        block = yaml.safe_dump(
            {"after_coverage": got}, allow_unicode=True, sort_keys=False, width=100
        )
        if "after_coverage:" in text:
            text = re.sub(r"after_coverage:\n(?:  [^\n]*\n)+", block, text, count=1)
        else:
            # `source:` 블록 앞에 넣는다 — B-1 필드와 같은 자리(케이스 메타)다.
            text = text.replace("\nsource:\n", "\n" + block + "\nsource:\n", 1)
        path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
