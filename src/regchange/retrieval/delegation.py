"""문서 간 위임 관계 — **제1조 본문에서 뽑는다** (R-22).

목적:
    사내 규정 문서가 스스로 선언한 위임 관계(`이 지침은 「정보보호정책」(ISP-POL-001)에서
    위임된 사항 …`)를 조 본문에서 추출해 문서 사이의 방향 그래프를 만든다. 이 그래프가
    검색 승격(`retrieval/promote.py`)의 유일한 근거다.

구현 이유:
    **본문이 정본이고 관계는 그 파생이다.** 위임을 프론트매터 같은 메타데이터에 따로
    적으면 같은 사실이 두 곳에 존재하게 되고, 두 곳은 어긋난다. 어긋났을 때 어느 쪽이
    맞는지 판정할 근거도 없다. `text_raw` 를 정본으로 두고 `text_norm` 을 파생으로 둔
    것(ADR-002)과 같은 방향이며, 여기서는 한 걸음 더 나아간다 — **메타데이터에 적으면
    우리가 만든 규약이고, 본문에서 뽑으면 문서가 스스로 말하는 것이다.**

    **`doc_id` 접두사(POL/GUIDE/PROC)로 계층을 유도하지 않는다.** 그것은 문서의 내용이
    아니라 우리의 명명 관례이며, 접두사를 다르게 붙인 문서가 하나 추가되면 조용히 깨진다.
    조용히 깨지는 규칙은 규칙이 아니다.

    **파싱이므로 ADR-007 이 적용된다.** 모든 간선에 `source=PARSED` 를 붙이고, 뽑히지
    않은 문서를 `undeclared` / `missing_article` 로 **따로 세어 돌려준다.** 0건을
    빈 결과로 뭉개면 "위임이 없는 문서"와 "표기가 바뀌어 못 뽑은 문서"가 같은 부재가
    된다 — 후자는 승격 경로 전체를 조용히 무력화한다.

    **순환은 실패다.** 지금 코퍼스에는 없지만 문서가 늘면 가능하고, 순환이 있으면 승격이
    끝나지 않거나(무한) 방문 순서에 따라 결과가 달라진다(비결정). 둘 다 감사에서 설명할
    수 없는 출력이므로 그래프를 만들지 않고 예외를 던진다.

트레이드오프:
    - **제1조만 본다.** 위임 선언이 다른 조에 있으면 놓친다. 대상을 넓히면 본문 곳곳의
      "…가 정하는 바에 따른다"(적용 관계)까지 위임으로 잡히고, 그것은 위임이 아니라
      준용이다. 놓치는 쪽을 골랐고, 놓친 사실은 `undeclared` 로 드러난다 — 조용히
      섞이는 것보다 세어지는 누락이 낫다.
    - 정규식 하나에 표기를 고정한다. 코퍼스 작성 규칙(`evals/corpus/internal-policies/
      README.md`)이 조 경계 표기를 한 형식으로 못박은 것과 같은 판단이다. 표기가 흔들리면
      `undeclared` 가 늘어나며, 그 수를 검사하는 것이 적재 시점의 방어다.
    - 위임의 **의미**를 판정하지 않는다. "위임된 사항"이라고 쓰여 있으면 위임으로 본다.
      실제로 그 문서가 상위 문서의 어느 부분을 위임받았는지는 본문이 말하지 않는 한
      알 수 없고, 추측하면 근거 없는 관계가 만들어진다.

엣지 케이스:
    - **제1조에 위임 선언이 없음**: `undeclared` 에 담는다. `ISP-POL-001` 은 최상위
      문서이므로 이것이 정상이다 — 정상과 이상을 코드가 가르지 않고 호출부가 판단한다.
    - **제1조 자체가 없음**: `missing_article` 에 담는다. `undeclared` 와 구별한다 —
      전자는 파서·적재를 의심할 사실이고 후자는 문서의 성질이다.
    - **한 조에 위임 선언이 둘 이상**: 전부 간선으로 만든다. 하나만 취하면 어느 것을
      버렸는지가 기록에 남지 않는다.
    - **상위 문서를 우리가 갖고 있지 않음**: `dangling` 에 담는다. 오류가 아니다 —
      아직 적재하지 않은 문서일 수 있다. 승격은 그 간선을 건너뛰며, 건너뛴 사실이 남는다.
    - **자기 자신을 위임 대상으로 지목**: 순환이므로 `DelegationError`.
    - **조 번호가 함께 적힌 위임**(`(ISP-GUIDE-002) 제35조에서 위임된`): `parent_article_no`
      에 담는다. 이 값을 버리지 않는다 — 문서가 스스로 지목한 조는 우리 검색보다 정확하다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

DELEGATION_ARTICLE_NO = 1
"""위임 선언을 찾는 조 번호.

법령도 사내 규정도 목적·근거를 제1조에 둔다. 이 코퍼스 5종 중 위임을 선언한 4종이
모두 제1조에 적었다 (`ISP-GUIDE-002`·`003`, `ISP-PROC-001`·`002`). 상수로 두는 이유는
값을 바꾸는 것이 곧 "어디를 정본으로 볼 것인가"의 변경이기 때문이다."""

_DELEGATION_PATTERN = re.compile(
    r"「(?P<title>[^」\n]{1,60})」\s*"
    r"\((?P<doc_id>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\)"
    r"(?:\s*제(?P<article_no>\d+)조)?"
    r"\s*에서\s*위임"
)
"""위임 선언 표기.

`「문서명」(DOC-ID)[ 제N조]에서 위임…` 한 형태만 받는다. 문서명과 ID 를 함께 요구하는
이유는 ID 없이 문서명만 적힌 문장이 준용·참조에도 쓰이기 때문이며, ID 가 있어야 우리가
가진 문서와 대조할 수 있다."""


class DelegationError(RuntimeError):
    """위임 그래프를 만들 수 없다. 순환이 대표적이며, 조용히 무시하지 않는다."""


class DelegationSource(StrEnum):
    """위임 관계를 무엇에서 얻었는가 (ADR-007 의 출처 표시와 같은 계열).

    지금은 값이 하나다. 그럼에도 enum 으로 두는 이유는, 나중에 다른 경로(사람이 등록한
    관계, 상위 시스템에서 받은 목록)가 생겼을 때 **기존 관계가 어디서 왔는지 소급해서
    알 수 없게 되는 것**을 막기 위해서다. 값이 하나인 동안에도 기록은 남는다.
    """

    PARSED = "PARSED"
    """문서 본문에서 파싱했다. 근거 문장이 `evidence_quote` 에 남는다."""


@dataclass(frozen=True, slots=True)
class DelegationEdge:
    """하위 문서 → 상위 문서 위임 간선 하나.

    `parent_article_no` 가 있으면 문서가 상위 문서의 **조까지 지목한** 것이다.
    그 경우 승격은 재검색하지 않고 그 조를 바로 올린다 (`promote.py`).
    """

    child_doc_id: str
    child_article_no: int
    parent_doc_id: str
    parent_article_no: int | None
    parent_title: str
    evidence_quote: str
    """근거가 된 문장. 검토 화면에서 "왜 이 조항이 올라왔는가"에 답하는 값이다."""
    source: DelegationSource = DelegationSource.PARSED


@dataclass(frozen=True, slots=True)
class DelegationGraph:
    """문서 사이의 위임 관계 전체와, 관계를 만들지 못한 문서 목록.

    목적:
        승격이 참조하는 유일한 관계 원장이면서, **뽑히지 않은 것을 함께 들고 다닌다.**

    구현 이유:
        `undeclared` / `missing_article` / `dangling` 을 결과에 담는다. 실패를 반환값에
        넣지 않으면 호출부는 "간선이 3개 나왔다"만 보고, 원래 5개여야 했다는 사실을
        영원히 모른다. `ops_run` 이 실패한 실행도 행으로 남기는 것과 같은 판단이다.

    트레이드오프:
        결과 객체가 커진다. 대신 승격 로그와 측정 리포트가 "무엇이 빠졌는가"를 다시
        계산하지 않아도 된다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    edges: tuple[DelegationEdge, ...]
    undeclared: tuple[str, ...]
    """제1조는 있으나 위임 선언이 없는 문서. 최상위 문서에서는 정상이다."""
    missing_article: tuple[str, ...]
    """제1조 자체가 없는 문서. 적재나 파서를 의심할 사실이다."""
    dangling: tuple[DelegationEdge, ...]
    """상위 문서를 우리가 갖고 있지 않은 간선. 승격에서 건너뛴다."""

    def parents_of(self, doc_id: str) -> tuple[DelegationEdge, ...]:
        """이 문서가 위임받은 상위 간선들. 없으면 빈 튜플."""
        return tuple(edge for edge in self.edges if edge.child_doc_id == doc_id)


def parse_delegations(doc_id: str, article_no: int, text: str) -> tuple[DelegationEdge, ...]:
    """조 본문 하나에서 위임 선언을 전부 뽑는다.

    목적:
        문서가 스스로 적은 위임 관계를 근거 문장과 함께 값으로 바꾼다.

    구현 이유:
        근거 문장(`evidence_quote`)을 함께 담는다. 관계만 남기면 검토 화면에서 "왜 이
        조항이 올라왔는가"에 답할 수 없고, 답할 수 없는 승격은 담당자가 검증할 수 없다
        (원칙 2 와 같은 요구를 관계에 적용한 것이다).

    트레이드오프:
        문장 경계를 마침표로 자른다. 한 문장에 위임 선언이 둘 있으면 같은 문장이 두 번
        인용된다. 중복을 피하려고 범위를 좁히면 근거가 잘려 읽을 수 없게 된다.

    엣지 케이스:
        - 매칭 없음: 빈 튜플. 호출부가 `undeclared` 로 센다.
        - 자기 자신 지목: 여기서는 간선으로 만든다. 순환 판정은 그래프에서 한다 —
          한 조만 보고는 순환인지 알 수 없기 때문이다.
    """
    edges: list[DelegationEdge] = []
    for match in _DELEGATION_PATTERN.finditer(text):
        article = match.group("article_no")
        edges.append(
            DelegationEdge(
                child_doc_id=doc_id,
                child_article_no=article_no,
                parent_doc_id=match.group("doc_id"),
                parent_article_no=int(article) if article else None,
                parent_title=match.group("title").strip(),
                evidence_quote=_sentence_around(text, match.start(), match.end()),
            )
        )
    return tuple(edges)


def build_delegation_graph(first_articles: Mapping[str, str | None]) -> DelegationGraph:
    """문서별 제1조 본문에서 위임 그래프를 만든다. 순환이면 예외.

    목적:
        승격이 참조할 관계 원장을 만들고, 만들지 못한 것을 함께 돌려준다.

    구현 이유:
        입력을 `{doc_id: 제1조 본문 또는 None}` 으로 받는다. 문서 목록 전체를 받아야
        "제1조가 없는 문서"를 셀 수 있고, 본문만 받으면 그 사실이 사라진다.

        **상위 문서가 목록에 없으면 오류로 올리지 않는다.** 아직 적재하지 않은 문서일 수
        있으며, 그것은 파싱의 실패가 아니라 적재 범위의 사실이다. 대신 `dangling` 으로
        남겨 승격이 건너뛰었다는 것이 보이게 한다.

    트레이드오프:
        순환을 만나면 그래프를 통째로 만들지 않는다. 순환에 걸리지 않은 부분만 살려
        진행하는 것이 더 관대하지만, 그러면 "일부 문서만 승격이 동작하는" 상태가 되고
        그 상태는 측정에서 원인 불명의 편차로 나타난다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    edges: list[DelegationEdge] = []
    undeclared: list[str] = []
    missing: list[str] = []

    for doc_id in sorted(first_articles):
        text = first_articles[doc_id]
        if text is None:
            missing.append(doc_id)
            continue
        found = parse_delegations(doc_id, DELEGATION_ARTICLE_NO, text)
        if not found:
            undeclared.append(doc_id)
            continue
        edges.extend(found)

    _reject_cycles(edges)

    known = set(first_articles)
    dangling = tuple(edge for edge in edges if edge.parent_doc_id not in known)
    linked = tuple(edge for edge in edges if edge.parent_doc_id in known)
    return DelegationGraph(
        edges=linked,
        undeclared=tuple(undeclared),
        missing_article=tuple(missing),
        dangling=dangling,
    )


def _reject_cycles(edges: list[DelegationEdge]) -> None:
    """위임 그래프에 순환이 있으면 `DelegationError`. 있을 수 없는 일을 미리 막는다."""
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.child_doc_id, []).append(edge.parent_doc_id)

    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str, path: tuple[str, ...]) -> None:
        if node in visiting:
            cycle = " → ".join((*path, node))
            msg = f"위임 관계에 순환이 있다: {cycle}. 승격 순서가 정해지지 않으므로 중단한다"
            raise DelegationError(msg)
        if node in done:
            return
        visiting.add(node)
        for parent in outgoing.get(node, ()):
            walk(parent, (*path, node))
        visiting.discard(node)
        done.add(node)

    for start in sorted(outgoing):
        walk(start, ())


def _sentence_around(text: str, start: int, end: int) -> str:
    """매칭을 포함하는 문장을 잘라낸다. 근거로 읽을 수 있는 최소 단위다."""
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start))
    right_candidates = [pos for pos in (text.find(".", end), text.find("\n", end)) if pos != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return text[left + 1 : right].strip()
