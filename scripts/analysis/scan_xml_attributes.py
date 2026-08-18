"""픽스처 전수에서 XML 속성을 훑는다 — 파서가 놓친 값을 찾기 위해.

목적:
    `tests/fixtures/law_api/`의 모든 응답에서 **속성을 가진 요소**를 전수로 모아
    요소 경로·속성명·등장 횟수·값 예시를 계열별로 낸다.

구현 이유:
    법제처 응답은 값을 태그 텍스트와 XML 속성 두 곳에 둔다. 파서는 태그 텍스트를
    읽으므로 속성은 구조적으로 놓치기 쉽고, **실제로 세 번 놓쳤다** — 조문키
    (ADR-001), 소관부처코드(작업 3), 제개정구분(작업 4).

    속성 누락은 값이 비는 것이 아니라 **조용히 다른 값으로 동작한다.** 소관부처코드가
    없으면 코드로 조인하려던 것이 이름으로 조인하게 되는데, 이름은 조인 키가 아니다
    (ADR-009). 그런데 이름이 있으니 코드는 돌고 결과가 나오고 아무도 모른다.

    세 번 걸렸으면 패턴이므로 네 번째를 가정하고 전수로 훑는다.

    **계열별로 나눠 센다.** 뭉쳐 세면 target 마다 다른 스키마가 평균에 묻힌다 —
    `search_admrul_aml.xml`을 `<law>`로 세어 0건으로 오판한 전례가 있다
    (`tests/fixtures/law_api/README.md`).

트레이드오프:
    표준 라이브러리 파서를 쓴다. `src/`는 외부 HTTP 응답을 다루므로 `defusedxml`을
    쓰지만(ADR-012), 이 스크립트는 신뢰된 로컬 픽스처만 읽는다. 신뢰 경계가 다르며,
    그 구별을 코드 위치로 표현한다.

    일회성 도구이므로 `src/`로 승격하지 않는다. 결과는 스크립트가 아니라
    `docs/api-exploration/law-api-spec.md` §2.1의 표가 원본이다.

엣지 케이스:
    - 파싱 실패하는 픽스처(`error_unknown_target_empty.xml`): 건너뛰되 **건너뛴
      사실을 출력한다.** 조용히 빼면 "전수"가 전수가 아니게 된다.
    - 네임스페이스 속성(`xmlns:*`): ElementTree 가 속성으로 노출하지 않는다.
      관측되면 별도 표시한다.
    - 속성이 0건인 계열: 그 자체가 결과다. 0을 오류로 다루지 않되 **의심 대상으로
      표시**한다 — 재계산 판별 원칙(silent-undercounting.md).
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "law_api"

MAX_EXAMPLES = 3
"""값 예시 개수. 형태를 알아보기에 충분하고 표가 길어지지 않는 값."""


def series_of(root: ET.Element, target: str) -> str:
    """루트 태그와 echo 된 target 으로 계열을 정한다.

    루트 태그만으로는 갈리지 않는다 — 검색과 일자별 이력이 둘 다 `LawSearch` 다
    (edge-case: `LAW_SEARCH` 와 `EFLAW_SEARCH` 는 루트가 완전히 같다).
    """
    if root.tag == "법령":
        return "DOCUMENT (본문 law/eflaw)"
    if root.tag == "AdmRulService":
        return "DOCUMENT (행정규칙 본문)"
    if root.tag == "AdmRulSearch":
        return "SEARCH (행정규칙 목록)"
    if root.tag == "LawService":
        return f"HISTORY (조문별 {target})"
    if root.tag == "LawSearch":
        if target in {"lsJoHstInf", "lsHstInf"}:
            return f"HISTORY (일자별 {target})"
        return f"SEARCH (목록 {target})"
    return f"OTHER ({root.tag})"


def walk(
    element: ET.Element,
    path: str,
    sink: dict[tuple[str, str, str], dict[str, object]],
    series: str,
    fixture: str,
) -> None:
    """요소를 재귀 순회하며 속성을 모은다. 경로는 루트부터의 태그 경로다."""
    for name, value in element.attrib.items():
        key = (series, path, name)
        entry = sink.setdefault(key, {"count": 0, "examples": [], "fixtures": set()})
        entry["count"] = int(entry["count"]) + 1  # type: ignore[call-overload]
        examples: list[str] = entry["examples"]  # type: ignore[assignment]
        if value not in examples and len(examples) < MAX_EXAMPLES:
            examples.append(value)
        fixtures: set[str] = entry["fixtures"]  # type: ignore[assignment]
        fixtures.add(fixture)

    for child in element:
        walk(child, f"{path}/{child.tag}", sink, series, fixture)


def main() -> int:
    if not FIXTURES.is_dir():
        print(f"픽스처 디렉터리가 없다: {FIXTURES}", file=sys.stderr)
        return 1

    sink: dict[tuple[str, str, str], dict[str, object]] = {}
    per_series_files: dict[str, int] = defaultdict(int)
    per_series_elements: dict[str, int] = defaultdict(int)
    skipped: list[str] = []
    files = sorted(FIXTURES.glob("*.xml"))

    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            skipped.append(f"{path.name}: {exc}")
            continue
        target = (root.findtext("target") or root.findtext(".//target") or "").strip()
        series = series_of(root, target)
        per_series_files[series] += 1
        per_series_elements[series] += sum(1 for _ in root.iter())
        walk(root, root.tag, sink, series, path.name)

    print(f"픽스처 {len(files)}개 중 {len(files) - len(skipped)}개 파싱")
    if skipped:
        print("건너뛴 파일 (조용히 빼지 않는다):")
        for line in skipped:
            print(f"  {line}")
    print()

    print(f"{'계열':32} {'파일':>4} {'요소':>7} {'속성종류':>8} {'속성총계':>8}")
    for series in sorted(per_series_files):
        kinds = [k for k in sink if k[0] == series]
        total = sum(int(sink[k]["count"]) for k in kinds)  # type: ignore[call-overload]
        flag = "   ← 0건, 의심 대상" if not kinds else ""
        print(
            f"{series:32} {per_series_files[series]:4} {per_series_elements[series]:7} "
            f"{len(kinds):8} {total:8}{flag}"
        )
    print()

    _report_header_tags()

    for series in sorted(per_series_files):
        keys = sorted(k for k in sink if k[0] == series)
        print(f"=== {series} ===")
        if not keys:
            print("  속성 0건")
            print()
            continue
        for _, elem_path, attr in keys:
            entry = sink[(series, elem_path, attr)]
            examples = ", ".join(str(v) for v in entry["examples"])  # type: ignore[arg-type]
            fixtures: set[str] = entry["fixtures"]  # type: ignore[assignment]
            print(
                f"  {elem_path:44} @{attr:16} {entry['count']:>6}회  "
                f"예시: {examples[:70]}  ({len(fixtures)}개 파일)"
            )
        print()
    return 0


def _report_header_tags() -> None:
    """본문 `<기본정보>`의 자식 태그와 파서 추출 여부를 대조한다.

    **속성만 훑으면 세 사고 중 하나를 못 잡는다.** 조문키와 소관부처코드는 속성이지만
    `제개정구분`은 **태그**였고, 파서가 그냥 읽지 않고 있었다. 누락의 공통점은
    "속성이라서"가 아니라 **"응답에 있는데 파서가 추출하지 않아서"**다.
    """
    read_by_parser = {
        "법령ID": "law_id",
        "법령명_한글": "law_name",
        "법종구분": "law_kind",
        "소관부처": "ministry / ministry_code(속성)",
        "제개정구분": "revision_kind",
        "공포일자": "promulgation_date",
        "시행일자": "document_effective_date",
    }
    tags: dict[str, int] = defaultdict(int)
    files = 0
    for path in sorted(FIXTURES.glob("*.xml")):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        if root.tag != "법령":
            continue
        basic = root.find("기본정보")
        if basic is None:
            continue
        files += 1
        for child in basic:
            tags[child.tag] += 1

    print(f"=== 본문 <기본정보> 태그 대조 (본문 픽스처 {files}개) ===")
    for tag in sorted(tags, key=lambda t: (-tags[t], t)):
        mark = f"읽음 → {read_by_parser[tag]}" if tag in read_by_parser else "**안 읽음**"
        print(f"  {tag:16} {tags[tag]:3}개 파일  {mark}")
    missing = [t for t in read_by_parser if t not in tags]
    if missing:
        print(f"  경고: 파서가 읽는다고 적힌 태그가 픽스처에 없다: {missing}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
