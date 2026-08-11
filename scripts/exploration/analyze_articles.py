"""저장된 법령 픽스처에서 조문키 체계와 구조 통계를 뽑는다 (0.7단계 탐색용).

목적:
    조문키 = [조문번호 4][가지번호 2][유형 1] 가설을 여러 법령으로 검증하고,
    반례(자릿수 초과, 유형 코드 추가, 필드 누락)를 찾는다.

구현 이유:
    가설 검증은 눈으로 몇 건 보는 것으로 끝내면 안 된다. 전수로 재구성해
    불일치 건수를 세야 반례가 드러난다.

트레이드오프:
    픽스처 전수를 메모리에 올린다. 탐색용 스크립트이므로 문제되지 않는다.

엣지 케이스:
    - 조문가지번호 태그가 없는 조문: 가지번호 0으로 간주해 재구성한다.
    - 조문키 길이가 7이 아닌 경우: 별도로 수집해 보고한다.
"""

from __future__ import annotations

import collections
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "law_api"


def text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def analyze(path: Path) -> None:
    root = ET.parse(path).getroot()
    name = root.findtext(".//법령명_한글") or root.findtext(".//법령명한글") or path.name

    keys: list[str] = []
    mismatches: list[str] = []
    key_lengths: collections.Counter[int] = collections.Counter()
    type_digits: collections.Counter[str] = collections.Counter()
    yeobu: collections.Counter[str] = collections.Counter()
    max_no = 0
    max_branch = 0
    moved: list[tuple[str, str, str]] = []
    changed = 0
    hang_missing_num = 0
    articles_without_hang = 0
    mok_outside_ho = 0
    eff_dates: collections.Counter[str] = collections.Counter()

    for art in root.iter("조문단위"):
        key = art.get("조문키") or ""
        keys.append(key)
        key_lengths[len(key)] += 1

        no = text(art.find("조문번호"))
        branch = text(art.find("조문가지번호")) or "0"
        flag = text(art.find("조문여부"))
        yeobu[flag] += 1
        eff_dates[text(art.find("조문시행일자"))] += 1

        if len(key) == 7:
            type_digits[key[6]] += 1
            rebuilt = f"{int(no):04d}{int(branch):02d}{key[6]}"
            if rebuilt != key:
                mismatches.append(f"{key} != rebuilt {rebuilt} (번호={no}, 가지={branch})")

        max_no = max(max_no, int(no) if no.isdigit() else 0)
        max_branch = max(max_branch, int(branch) if branch.isdigit() else 0)

        before, after = text(art.find("조문이동이전")), text(art.find("조문이동이후"))
        if (before or after) and (before != "000000" or after != "000000"):
            moved.append((key, before, after))
        if text(art.find("조문변경여부")) == "Y":
            changed += 1

        hangs = art.findall("항")
        if flag == "조문" and not hangs:
            articles_without_hang += 1
        for hang in hangs:
            if hang.find("항번호") is None:
                hang_missing_num += 1
            seen_ho = False
            for child in hang:
                if child.tag == "호":
                    seen_ho = True
                elif child.tag == "목" and not seen_ho:
                    mok_outside_ho += 1

    print(f"\n=== {name} ({path.name}) ===")
    print(f"조문단위: {len(keys)}  조문키 길이 분포: {dict(key_lengths)}")
    print(f"조문여부 값: {dict(yeobu)}")
    print(f"7번째 자리(유형) 분포: {dict(type_digits)}")
    print(f"최대 조문번호: {max_no}  최대 가지번호: {max_branch}")
    print(f"조문키 재구성 불일치: {len(mismatches)}")
    for m in mismatches[:5]:
        print(f"   {m}")
    print(f"조문변경여부=Y: {changed}   이동 필드 비영값: {len(moved)}")
    for m in moved[:5]:
        print(f"   조문키={m[0]} 이전={m[1]!r} 이후={m[2]!r}")
    print(f"항번호 없는 항: {hang_missing_num}   항 태그 없는 조문: {articles_without_hang}")
    print(f"선행 호 없이 등장한 목: {mok_outside_ho}")
    print(f"조문시행일자 종류: {len(eff_dates)} -> {dict(list(eff_dates.most_common(5)))}")


if __name__ == "__main__":
    targets = sys.argv[1:] or sorted(str(p) for p in FIXTURES.glob("law_*.xml"))
    for target in targets:
        analyze(Path(target))
