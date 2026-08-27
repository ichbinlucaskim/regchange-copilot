"""인용 적합도 판정지 — **사람이 채울 표를 만든다** (`docs/23-metrics-summary.md` §2).

    uv run --group eval python -m evals.runners.citation_adequacy

무엇을 하는가:
    gate 3단이 `SUPPORTED` 라고 판정한 (주장, 인용) 쌍을 **전수** 뽑아, 사람이
    같은 척도로 다시 판정할 표를 만든다. 두 판정이 갈리는 비율이 F-6("시스템이
    그럴듯하게 틀림")의 실측치다.

    **LLM 을 부르지 않는다. 비용 0.** 판정은 사람이 한다 — Claude Code 가 채우면
    그것도 LLM 판정이고, 그러면 「LLM 이 놓친 것을 LLM 으로 재는」 순환이 된다.

──────────────────────────────────────────────────────────────────────────────
왜 전수인가 — **표본이 아니라 모집단이다**
──────────────────────────────────────────────────────────────────────────────

F-6 의 분모는 「gate 3단이 `SUPPORTED` 라 한 판정」이고, 42건 실행에서 그 수는
**20** 이다(`PARTIAL` 24 · `UNSUPPORTED` 7). 20 은 사람이 다 볼 수 있는 양이므로
표본을 뽑을 이유가 없다. 전수로 하면 두 가지가 따라온다.

  1. **표집오차가 0 이다.** 남는 불확실성은 「이 42건이 운영을 대표하는가」뿐이며,
     그것은 표본 크기로 줄일 수 있는 종류가 아니다.
  2. **판정 열이 상수가 되어 앵커링이 사라진다.** 등급을 섞어 뽑으면 사람이
     gate 의 등급을 보고 판정하게 되고, 그것은 이 저장소가 de-anchored 검증기를
     만들며 문제 삼았던 바로 그 기전이다(`docs/12` §12). 20 행이 전부 `SUPPORTED`
     이면 그 열은 아무것도 구별해 주지 않는다.

**`PARTIAL` 24건과 `UNSUPPORTED` 7건은 이번에 판정하지 않는다.** 둘 다 볼 값이
있지만(전자는 담당자에게 그대로 가고, 후자는 제거되어 재현율을 깎는다) 지시가
정의한 F-6 의 분모가 아니다. 세 등급을 한 번에 채우면 판정 열이 정보를 갖게 되어
위 2번이 깨진다. **다음 측정으로 남긴다.**

──────────────────────────────────────────────────────────────────────────────
표에 무엇을 넣는가 — **검증기가 본 것보다 많이 준다**
──────────────────────────────────────────────────────────────────────────────

gate 3단이 받는 것은 「인용 문단 표기 + 인용문 + 주장」 세 조각뿐이다
(`prompts/grounding.py`). **문단 전문은 보지 않는다 — 인용문 한 문장만 본다.**

사람에게는 **문단 전문을 함께 준다.** case-013 이 그 이유다 — 문단이 실재하고
인용문도 원문 그대로인데 의미가 어긋났고, gate 2단도 3단도 잡지 못했다. 인용문만
보면 사람도 같은 것을 본다.

**gate 3단의 판정 사유(`reason`)는 판정지에 넣지 않는다.** 등급은 상수라 앵커가
되지 않지만 사유 문장은 앵커가 된다. 사유는 결과 JSON 에 남으므로 판정을 채운 뒤
대조할 수 있다 — 순서가 요점이다.

──────────────────────────────────────────────────────────────────────────────
어디서 읽는가 — **운영이 쓰는 경로로만 읽는다**
──────────────────────────────────────────────────────────────────────────────

주장·인용문의 원본은 결과 JSON 에 없다(집계값만 있다). 경로는 이렇다.

    결과파일.assessment_id
      → llm_invocation (impact_assessment_id, purpose='IMPACT_ASSESSMENT', 최종 revision)
      → s3_key_raw_output → 저장소 블롭 → 초안 JSON
      → impacts[i] / departments[i]  (판정 키 `impact:i` / `dept:i` 와 같은 색인)
      → paragraph_id(UUID) → policy_paragraph → DOC#조번호 · 조 제목 · text_raw

`docs/21` §2.1 과 같은 경로다. **다시 돌리지 않는다** — 다시 돌리면 그때의 초안이
아니라 지금의 초안을 보게 되고, R-27 이 케이스 단위 불안정을 실측한 이상 그 둘은
같은 것이 아니다.

트레이드오프:
    저장소 블롭이 사라지면 이 러너는 아무것도 만들지 못한다. 해시(`raw_output_sha256`)
    가 DB 에 있으므로 **바뀐 것은 드러나지만 없어진 것은 복구되지 않는다.** 초안을
    DB 컬럼으로 복제해 두는 대안을 택하지 않은 것은 ADR-007 의 결정이다.

엣지 케이스:
    - 호출 기록이 없는 `assessment_id`: 그 케이스를 건너뛰지 않고 `failures` 에
      `NO_INVOCATION` 으로 남긴다. 건너뛰면 분모가 조용히 줄어든다.
    - 블롭이 없는 기록: `MISSING_BLOB`. 위와 같은 이유다.
    - 초안의 인용 문단 UUID 가 `policy_paragraph` 에 없음: `UNKNOWN_PARAGRAPH`.
      코퍼스 세대가 어긋난 것이며 표를 만들면 안 되는 상태다.
    - `SUPPORTED` 가 0건: 표가 비고 `sheet_rows=0` 이 남는다. 오류가 아니라
      「판정할 것이 없었다」이며 F-6 의 분모가 0 이라는 뜻이다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from evals.runners.power_analysis import clopper_pearson
from regchange.adapters.storage.local import DocumentStoreError, LocalDocumentStore
from regchange.config.settings import apply_dotenv, snapshot_root
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"

TARGET_LEVEL = "SUPPORTED"
"""판정 대상 등급. **F-6 의 분모가 이 등급이다** — 「gate 가 뒷받침된다고 한 것 중
사람이 아니라고 보는 비율」이 지시가 정의한 값이다."""

LEVELS = ("SUPPORTED", "PARTIAL", "UNSUPPORTED")
"""사람이 쓸 수 있는 등급. **gate 3단과 같은 척도여야 대조가 된다** —
다른 어휘를 쓰면 두 판정을 나란히 놓을 수 없다."""

READING_POINTS = (0, 1, 2, 4)
"""판정 전에 신뢰구간을 미리 보여 줄 지점.

0 이 핵심이다 — **0 이 나와도 상한이 얼마인지**를 채우기 전에 못박기 위한 표이며,
1·2·4 는 그 상한이 관측 수에 따라 어떻게 움직이는지를 보이는 대조점이다.
분모보다 큰 지점은 건너뛴다."""

SHUFFLE_SEED = 20260826
"""판정 순서를 섞는 시드. **값 자체에 의미는 없고 기록된다는 것이 요점이다.**

작성일(YYYYMMDD)을 쓴다 — 임의의 수를 고르면 「왜 그 수인가」에 답할 수 없고,
날짜는 재현 가능하며 다음 측정에서 자연히 다른 값이 된다. 이 시드와 행 식별자의
SHA-256 이 순서를 정하므로 같은 입력에 항상 같은 순서가 나온다."""

DRAFT_PURPOSE = "IMPACT_ASSESSMENT"
"""초안 호출의 `purpose`. 검증 호출(`CITATION_VERIFICATION`)과 구별해야 한다 —
같은 `impact_assessment_id` 아래 둘 다 있다."""

INVOCATION_QUERY = """
SELECT s3_key_raw_output, revision
  FROM llm_invocation
 WHERE impact_assessment_id = %(assessment_id)s
   AND purpose = %(purpose)s
   AND outcome = 'OK'
 ORDER BY revision DESC, invoked_at DESC
 LIMIT 1
"""

PARAGRAPH_QUERY = """
SELECT p.id, d.doc_id, p.article_no, p.article_title, p.text_raw
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
 WHERE p.id = ANY(%(ids)s) AND p.known_until = 'infinity'
"""

logger = logging.getLogger("citation_adequacy")


@dataclass(frozen=True, slots=True)
class Row:
    """판정지 한 행 — (주장, 인용) 쌍 하나.

    목적:
        사람이 판정하는 데 필요한 것과 사후 대조에 필요한 것을 한 값으로 담는다.

    구현 이유:
        `gate_reason` 을 들고 다니되 판정지에는 쓰지 않는다. 두 용도를 다른 타입으로
        가르면 같은 행을 두 번 만들어야 하고, 그때 둘이 어긋날 수 있다.

    트레이드오프:
        판정지 출력 함수가 「무엇을 빼는가」를 알아야 한다. 타입이 강제하지 못하는
        규칙이며, 그래서 그 함수의 docstring 에 근거를 적는다.

    엣지 케이스:
        - 인용 문단이 조회되지 않은 행: `paragraph_text` 가 빈 문자열이 아니라
          이 행 자체가 만들어지지 않는다. 호출부가 `failures` 로 보낸다.
    """

    case_id: str
    case_type: str
    expected_outcome: str
    impact_status: str
    key: str
    claim: str
    quote: str
    spec: str
    paragraph_id: str
    paragraph_text: str
    gate_level: str
    gate_reason: str


def claim_of(draft: dict[str, Any], key: str) -> tuple[str, str, str]:
    """판정 키가 가리키는 (주장, 인용문, 문단 UUID) 를 초안에서 꺼낸다.

    목적:
        `impact:3` · `dept:1` 같은 키를 초안의 실제 문장으로 되돌린다.

    구현 이유:
        키를 만드는 곳(`pipeline/impact.py`)과 같은 색인 규칙을 쓴다. 부서 주장의
        문장도 그곳이 조립한 형태(`{부서}가 관여한다: {근거}`)를 그대로 재현한다 —
        검증기가 판정한 문장이 그것이고, 다른 문장을 보여 주면 대조가 성립하지 않는다.

    트레이드오프:
        두 곳에 같은 조립 규칙이 생긴다. 파이프라인에서 함수를 꺼내 공유하는 대안은
        택하지 않았다 — 러너가 도메인 코드를 끌어다 쓰면 `evals` 가 `pipeline` 의
        내부 형식에 묶인다.

    엣지 케이스:
        - 알 수 없는 접두사: `ValueError`. 키 규칙이 바뀐 것이며 조용히 건너뛰면
          판정 대상이 줄어든다.
        - 색인이 범위 밖: `IndexError` 를 그대로 올린다. 초안과 판정이 어긋난 것이다.
    """
    kind, _, raw_index = key.partition(":")
    index = int(raw_index)
    if kind == "impact":
        item = draft["impacts"][index]
        return str(item["claim"]), str(item["quote"]), str(item["paragraph_id"])
    if kind == "dept":
        entry = draft["departments"][index]
        claim = f"{entry['department']}가 관여한다: {entry['rationale']}"
        return claim, str(entry["basis_quote"]), str(entry["basis_paragraph_id"])
    msg = f"알 수 없는 판정 키: {key}"
    raise ValueError(msg)


async def collect(data: dict[str, Any]) -> tuple[list[Row], list[dict[str, str]]]:
    """결과 파일에서 대상 등급의 판정을 전수 모은다.

    목적:
        판정지의 재료를 만든다.

    구현 이유:
        DB 와 저장소를 한 번씩만 지나도록 문단 조회를 마지막에 일괄로 한다. 행마다
        조회하면 20 회 왕복이 되고, 더 중요하게는 **어느 문단이 없는지**를 한 곳에서
        판정하지 못한다.

    트레이드오프:
        초안 블롭은 케이스마다 읽는다(일괄로 묶을 수 없다). 42건이면 최대 42회
        파일 읽기이며 로컬에서 문제가 되지 않는다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    failures: list[dict[str, str]] = []
    pending: list[tuple[dict[str, Any], dict[str, Any], str, str, str]] = []
    store = LocalDocumentStore(snapshot_root())

    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        for case in data["cases"]:
            targets = [j for j in case["judgments"] if j["level"] == TARGET_LEVEL]
            if not targets:
                continue
            cursor = await conn.execute(
                INVOCATION_QUERY,
                {"assessment_id": case["assessment_id"], "purpose": DRAFT_PURPOSE},
            )
            record = await cursor.fetchone()
            if record is None:
                failures.append({"case_id": case["case_id"], "reason": "NO_INVOCATION"})
                continue
            key = str(record[0])
            try:
                body = await store.get(key)
            except DocumentStoreError:
                logger.warning("%s: 초안 블롭이 없다 (%s)", case["case_id"], key)
                failures.append(
                    {"case_id": str(case["case_id"]), "reason": "MISSING_BLOB", "key": key}
                )
                continue
            draft: dict[str, Any] = json.loads(body.decode("utf-8"))
            for judgment in targets:
                claim, quote, paragraph_id = claim_of(draft, str(judgment["key"]))
                pending.append((case, judgment, claim, quote, paragraph_id))

        ids = sorted({paragraph_id for _, _, _, _, paragraph_id in pending})
        cursor = await conn.execute(PARAGRAPH_QUERY, {"ids": ids})
        paragraphs = {
            str(row[0]): (f"{row[1]} 제{row[2]}조({row[3]})", str(row[4]))
            for row in await cursor.fetchall()
        }

    rows: list[Row] = []
    for case, judgment, claim, quote, paragraph_id in pending:
        found = paragraphs.get(paragraph_id)
        if found is None:
            failures.append(
                {
                    "case_id": str(case["case_id"]),
                    "reason": "UNKNOWN_PARAGRAPH",
                    "paragraph_id": paragraph_id,
                }
            )
            continue
        spec, text = found
        rows.append(
            Row(
                case_id=str(case["case_id"]),
                case_type=str(case.get("case_type", "")),
                expected_outcome=str(case["expected_outcome"]),
                impact_status=str(case["impact_status"]),
                key=str(judgment["key"]),
                claim=claim,
                quote=quote,
                spec=spec,
                paragraph_id=paragraph_id,
                paragraph_text=text,
                gate_level=str(judgment["level"]),
                gate_reason=str(judgment["reason"]),
            )
        )
    return rows, failures


def shuffle(rows: list[Row]) -> list[Row]:
    """판정 순서를 섞는다 — **같은 원천의 케이스가 붙어 있지 않게** 한다.

    목적:
        판정 순서에 의한 이월 효과를 끊는다. 케이스 순서대로 두면 같은 원천에서
        나온 케이스가 연속으로 오고(IMPACT 20 중 15건이 두 원천), 앞 판정이 뒤에
        영향을 준다.

    구현 이유:
        난수 생성기를 쓰지 않고 **`SHUFFLE_SEED` 와 행 식별자의 해시로 정렬한다.**
        같은 입력에 항상 같은 순서가 나오고(재현), 시드를 바꾸면 다른 순서가 나오며,
        결과 JSON 이 행마다 원래 색인을 들고 있으므로 **되돌릴 수 있다.**
        `random` 을 쓰면 파이썬 버전에 따라 순서가 달라질 여지가 남는다 —
        해시는 그 여지가 없다.

    트레이드오프:
        섞인 순서에서는 같은 케이스의 두 주장(`impact:0` 과 `dept:0`)이 떨어져
        놓이므로 사람이 문맥을 이어 보지 못한다. **그것이 목적이다** — 문맥을 이어
        보면 「이 케이스는 아까 맞았으니 이것도 맞겠지」가 생긴다.

    엣지 케이스:
        - 해시가 같은 두 행: 정렬이 안정 정렬이라 원래 순서를 유지한다. 오류가
          아니며 실질적으로 일어나지 않는다(256비트).
    """

    def order(row: Row) -> str:
        material = f"{SHUFFLE_SEED}:{row.case_id}:{row.key}".encode()
        return hashlib.sha256(material).hexdigest()

    return sorted(rows, key=order)


def render_sheet(rows: list[Row], result_file: Path) -> str:
    """사람이 채울 판정지를 마크다운으로 만든다.

    목적:
        판정에 필요한 것만 화면에 올린다.

    구현 이유:
        **기계가 이미 무엇이라 했는지를 한 조각도 싣지 않는다.** 빼는 것이 넷이다.

        | 뺀 것 | 왜 |
        |---|---|
        | gate 3단 등급 열 | 20행 전부 `SUPPORTED` 라 **행을 구별하지는 않지만**
          「기계가 다 통과시켰다」는 사전 확률을 만든다. 행을 구별하지 않는 것과
          앵커가 아닌 것은 다르다 |
        | gate 3단 판정 사유 | 문장 그대로 앵커다 |
        | 케이스 ID·유형 | `case_type` 이 골든셋 정답 라벨이다. 「이건 IMPACT
          케이스」를 알면 뒷받침한다고 보게 된다 |
        | 실행 판정(`OK`/`NEEDS_REVIEW`) | 또 하나의 기계 판정이다 |

        빠진 정보는 결과 JSON 의 `key` 절에 행 번호로 대응돼 있어 **채운 뒤에**
        대조된다. 순서가 요점이다.

        **개정 조문도 싣지 않는다.** 이 판정지가 묻는 것은 「이 문단이 이 주장을
        뒷받침하는가」이지 「이 개정이 이 문단에 영향을 주는가」가 아니다. 후자를
        물으려면 개정 조문이 필요하지만, 그것은 **gate 3단이 받지 않은 정보**이므로
        넣는 순간 두 판정이 다른 질문에 답하게 되고 대조가 성립하지 않는다.

    트레이드오프:
        표 하나에 담기에는 문단 전문이 길어 행마다 블록을 쓴다. 표로 압축하면
        전문을 잘라야 하고, 인용문만 보면 사람이 검증기와 같은 것을 보게 된다 —
        그 차이가 F-6 의 소재다.

    엣지 케이스:
        - 행이 0개: 표 자리에 「판정할 것이 없다」를 적는다. 빈 표를 내면 판정을
          안 한 것과 구별되지 않는다.
    """
    lines = [
        "# 인용 적합도 판정지 — **사람이 채운다**",
        "",
        f"**대상 실행**: `{result_file.name}`",
        f"**판정 대상**: **{len(rows)}건 전수**",
        f"**순서**: `SHUFFLE_SEED={SHUFFLE_SEED}` 으로 섞었다 (§순서 참조)",
        "",
        "> **이 문서에는 기계의 판정이 한 조각도 실려 있지 않다.** 등급도, 판정 사유도,",
        "> 케이스 유형도, 골든셋 정답도 없다. 전부 결과 JSON 에 행 번호로 대응돼 있고",
        "> **채운 뒤에** 대조한다.",
        "",
        "---",
        "",
        "## 무엇을 묻는가 — **한 가지만 묻는다**",
        "",
        "> **이 인용 문단이 이 주장을 뒷받침하는가.**",
        "",
        "묻지 **않는** 것:",
        "",
        "- 주장이 사실로서 옳은가",
        "- 이 개정이 이 문단에 실제로 영향을 주는가 ← **다른 질문이다.** 그것을"
        " 물으려면 개정 조문이 필요한데, 개정 조문은 기계 검증기도 받지 않았다."
        " 넣으면 두 판정이 서로 다른 질문에 답하게 되어 대조가 성립하지 않는다",
        "- 부서 배정이 타당한가 (부서 주장도 **문단이 그 근거를 담고 있는가**만 본다)",
        "",
        "## 판정 기준 3종 — 경계를 먼저 정한다",
        "",
        "| 등급 | 경계 |",
        "|---|---|",
        "| `SUPPORTED` | 주장이 **사내 규정에 대해 단언하는 내용**이 문단 안에서"
        " 확인된다. 표현이 달라도 된다 |",
        "| `PARTIAL` | 그 내용의 **일부만** 확인된다. 문단과 주장이 겹치되, 주장이"
        " 문단에 없는 조건·대상·범위를 하나 이상 더 담고 있다 |",
        "| `UNSUPPORTED` | 확인되지 않는다. 문단이 **다른 것**을 말하거나 주장이"
        " 문단에서 도출되지 않는다. 「인용문은 문단에 있는데 문단의 뜻이 주장과"
        " 다르다」가 여기다 |",
        "",
        "**경계에서 갈리는 두 경우를 미리 못박는다.**",
        "",
        "1. **주장이 개정 조문에 대해 말하는 부분**(「개정법이 24시간 이내를 요구한다」)은",
        "   사내 문단이 담을 수 없는 것이 당연하다. 그 부분이 문단에 없다는 이유만으로",
        "   `UNSUPPORTED` 로 내리지 않는다 — 그 경우는 `PARTIAL` 이다.",
        "2. **문단이 주장의 반대를 말하거나 대상이 다르면** 인용문이 원문 그대로여도",
        "   `UNSUPPORTED` 다. 인용문의 실재는 이미 코드가 확인했고, 여기서 묻는 것은 뜻이다.",
        "",
        "## 무엇을 주고 무엇을 안 주는가",
        "",
        "| | 기계 검증기가 본 것 | 사람이 보는 것 |",
        "|---|---|---|",
        "| 조 표기 | ○ | ○ |",
        "| **인용문 한 문장** | ○ | ○ |",
        "| **문단 전문** | ✗ | **○** |",
        "| 개정 조문 | ✗ | ✗ |",
        "| 초안의 나머지 | ✗ | ✗ |",
        "",
        "**문단 전문만 더 준다.** 인용문은 원문 그대로인데 문단의 뜻이 다른 경우를",
        "인용문만 보고는 가릴 수 없기 때문이며, 그것이 이 측정이 겨냥하는 실패다.",
        "",
        "## 순서",
        "",
        "케이스 순서대로 두면 같은 원천의 케이스가 붙어 앞 판정이 뒤에 영향을 준다",
        f"(IMPACT 20건 중 15건이 두 원천이다). `SHUFFLE_SEED={SHUFFLE_SEED}` 과 행",
        "식별자의 SHA-256 으로 정렬해 섞었다. 결과 JSON 이 행마다 원래 색인을 들고 있어",
        "**되돌릴 수 있다.**",
        "",
        "## 채운 뒤에 무엇이 계산되는가 — **미리 고정한다**",
        "",
        "**주 지표는 하나다.**",
        "",
        "```",
        "F-6 = 사람이 UNSUPPORTED 로 본 수 / 20",
        "```",
        "",
        "`PARTIAL` 은 **`UNSUPPORTED` 에 합치지 않는다.** 합치면 「뒷받침하지 않는다」와",
        "「일부만」이 한 칸에 들어가고, 그것이 이 저장소가 다섯 번 무너졌다고 기록한",
        "상태 뭉갬이다. gate 3단 자신도 두 등급을 다르게 다룬다 — `UNSUPPORTED` 는",
        "제거하고 `PARTIAL` 은 담당자에게 그대로 보낸다. 결과가 다르므로 지표도 나눈다.",
        "",
        "**함께 내되 주 지표로 쓰지 않는 값 둘**: 불일치율 `(PARTIAL+UNSUPPORTED)/20`,",
        "일치율 `SUPPORTED/20`.",
        "",
        "### 0 이 나와도 「F-6 이 없다」가 아니다",
        "",
        "95% 정확(Clopper-Pearson) 구간이다. **결과를 보고 해석을 정하지 않기 위해 먼저 적는다.**",
        "",
    ]
    if not rows:
        lines += [
            "**판정할 것이 없다.** 대상 등급의 판정이 0건이라 분모가 없다 —",
            "**분모 0 에는 구간도 비율도 주지 않는다.**",
            "",
        ]
        return "\n".join(lines)

    lines += [
        "| 사람이 `UNSUPPORTED` 로 본 수 | F-6 | 95% 구간 |",
        "|---|---|---|",
    ]
    total = len(rows)
    for successes in (s for s in READING_POINTS if s <= total):
        low, high = clopper_pearson(successes, total)
        lines.append(
            f"| {successes} / {total} | {successes / total:.4f} | ({low:.3f}, {high:.3f}) |"
        )
    lines += [
        "",
        "**0/20 의 상한이 0.168 이다.** 한 건도 안 나와도 실제 비율은 **17% 까지** 일 수",
        "있다. 「분모가 0 인 지표는 지표가 아니다」와 같은 종류의 주의이며, 분모가 20 이어도",
        "상한이 이 정도라는 사실을 채우기 전에 적어 둔다.",
        "",
        "---",
        "",
    ]
    lines += [
        "## 요약표 — **판정을 여기에 적는다**",
        "",
        "`SUPPORTED` / `PARTIAL` / `UNSUPPORTED` 중 하나를 적는다. 채점기가 이 표를 읽는다.",
        "",
        "| # | 인용 문단 | **판정** |",
        "|---|---|---|",
    ]
    for number, row in enumerate(rows, start=1):
        lines.append(f"| {number} | {row.spec} | |")
    lines += ["", "---", "", "## 항목", ""]

    for number, row in enumerate(rows, start=1):
        lines += [
            f"### {number}. {row.spec}",
            "",
            "**주장**",
            "",
            f"> {row.claim}",
            "",
            "**인용문 (검증기가 본 것)**",
            "",
            f"> {row.quote}",
            "",
            "**인용 문단 전문 (검증기가 보지 못한 것)**",
            "",
            "```",
            row.paragraph_text.strip(),
            "```",
            "",
            "**판정**: `SUPPORTED` / `PARTIAL` / `UNSUPPORTED` → ",
            "",
            "**근거 한 줄**: ",
            "",
            "---",
            "",
        ]
    return "\n".join(lines)


SUMMARY_ROW = re.compile(r"^\|\s*(\d+)\s*\|[^|]*\|\s*([A-Za-z_`]*)\s*\|\s*$")


def read_verdicts(sheet: Path, expected_rows: int) -> dict[int, str]:
    """채워진 판정지의 요약표를 읽는다 — **비어 있으면 조용히 넘기지 않는다**.

    목적:
        사람이 적은 등급을 행 번호로 회수한다.

    구현 이유:
        사람이 실제로 쓰는 자리가 요약표 한 곳이므로 그곳만 읽는다. 항목 블록의
        「판정:」 줄까지 읽으면 두 곳이 어긋났을 때 어느 쪽이 참인지 정할 수 없다.

    트레이드오프:
        마크다운 표를 정규식으로 읽으므로 표 모양이 바뀌면 깨진다. 깨질 때
        **조용히 적게 읽지 않고 예외로 멈춘다** — 판정 3건을 놓친 채 계산된
        F-6 은 틀린 값이 아니라 없는 값이다.

    엣지 케이스:
        - 빈 칸이 하나라도 있음: `ValueError` 로 몇 번 행이 비었는지 말한다.
        - 3등급 밖의 문자열: `ValueError`. 오타를 등급으로 읽지 않는다.
        - 행 수가 기대와 다름: `ValueError`. 판정지와 결과 파일의 세대가 다르다.
    """
    verdicts: dict[int, str] = {}
    blank: list[int] = []
    for line in sheet.read_text(encoding="utf-8").splitlines():
        match = SUMMARY_ROW.match(line)
        if match is None:
            continue
        number = int(match.group(1))
        level = match.group(2).strip().strip("`")
        if not level:
            blank.append(number)
            continue
        if level not in LEVELS:
            msg = f"{number}행: 알 수 없는 등급 {level!r}. {LEVELS} 중 하나여야 한다"
            raise ValueError(msg)
        verdicts[number] = level
    if len(verdicts) + len(blank) != expected_rows:
        msg = (
            f"요약표에서 {len(verdicts) + len(blank)}행을 읽었는데 {expected_rows}행이어야 한다. "
            "판정지와 결과 파일의 세대가 다르거나 표 모양이 바뀌었다"
        )
        raise ValueError(msg)
    if blank:
        msg = f"판정이 비어 있는 행: {blank}. 전수를 채워야 F-6 의 분모가 성립한다"
        raise ValueError(msg)
    return verdicts


def score(payload: dict[str, Any], verdicts: dict[int, str]) -> dict[str, Any]:
    """사람 판정을 F-6 실측치와 신뢰구간으로 집계한다.

    목적:
        「gate 3단이 `SUPPORTED` 라 한 것 중 사람이 `UNSUPPORTED` 로 보는 비율」을
        낸다.

    구현 이유:
        구간은 `power_analysis.clopper_pearson` 을 **재사용한다.** 두 곳에서 따로
        구현하면 §3 의 구간과 이 구간이 다른 방법으로 계산될 수 있다.

    트레이드오프:
        `PARTIAL` 처리 방침이 코드에 박혀 있다(합치지 않는다). 옵션으로 두지 않은
        이유는 **결과를 보고 방침을 바꾸는 경로를 만들지 않기 위해서다.**

    엣지 케이스:
        - 행 수 0: `ValueError`. 분모 0 에 비율을 주지 않는다.
        - 판정이 전부 `SUPPORTED`: F-6 = 0.0 이며 상한이 함께 나온다. **0 을
          「없다」로 읽지 않게 하는 것이 이 상한의 역할이다.**
    """
    rows: list[dict[str, Any]] = payload["rows"]
    if not rows:
        msg = "판정 대상이 0건이면 F-6 이 정의되지 않는다"
        raise ValueError(msg)
    tally = dict.fromkeys(LEVELS, 0)
    disagreements: list[dict[str, Any]] = []
    for row in rows:
        level = verdicts[int(row["sheet_row"])]
        tally[level] += 1
        if level != TARGET_LEVEL:
            disagreements.append(
                {
                    "sheet_row": row["sheet_row"],
                    "case_id": row["case_id"],
                    "key": row["key"],
                    "spec": row["spec"],
                    "human_level": level,
                    "gate_reason": row["gate_reason"],
                }
            )
    total = len(rows)
    unsupported = tally["UNSUPPORTED"]
    mismatch = unsupported + tally["PARTIAL"]
    low, high = clopper_pearson(unsupported, total)
    mismatch_low, mismatch_high = clopper_pearson(mismatch, total)
    return {
        "denominator": total,
        "human_tally": tally,
        "f6": round(unsupported / total, 4),
        "f6_ci95": [round(low, 4), round(high, 4)],
        "mismatch_rate": round(mismatch / total, 4),
        "mismatch_ci95": [round(mismatch_low, 4), round(mismatch_high, 4)],
        "agreement_rate": round(tally[TARGET_LEVEL] / total, 4),
        "partial_policy": "PARTIAL 을 UNSUPPORTED 에 합치지 않는다 (판정 전에 고정)",
        "disagreements": disagreements,
    }


def run(result_file: Path, out_json: Path, out_sheet: Path) -> None:
    """판정지와 결과 JSON 을 만든다.

    파일 입출력을 동기 함수에 모은다 — 비동기 함수 안에서 `Path` 를 만지면 이벤트
    루프를 막고, 그 규칙을 린터(`ASYNC240`)가 강제한다.
    """
    data: dict[str, Any] = json.loads(result_file.read_text(encoding="utf-8"))
    collected, failures = asyncio.run(collect(data))
    rows = shuffle(collected)
    original = {(row.case_id, row.key): index for index, row in enumerate(collected, start=1)}
    impact_rows = sum(1 for row in rows if row.key.startswith("impact:"))
    dept_rows = sum(1 for row in rows if row.key.startswith("dept:"))
    cases_covered = sorted({row.case_id for row in rows})
    payload = {
        "result_file": result_file.name,
        "target_level": TARGET_LEVEL,
        "shuffle_seed": SHUFFLE_SEED,
        "sheet": out_sheet.name,
        "sheet_rows": len(rows),
        "cases_covered": cases_covered,
        "by_kind": {"impact": impact_rows, "dept": dept_rows},
        "failures": failures,
        "rows": [
            {
                "sheet_row": index,
                "original_row": original[(row.case_id, row.key)],
                "case_id": row.case_id,
                "case_type": row.case_type,
                "expected_outcome": row.expected_outcome,
                "impact_status": row.impact_status,
                "key": row.key,
                "spec": row.spec,
                "paragraph_id": row.paragraph_id,
                "claim": row.claim,
                "quote": row.quote,
                "gate_level": row.gate_level,
                "gate_reason": row.gate_reason,
                "human_level": None,
            }
            for index, row in enumerate(rows, start=1)
        ],
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_sheet.write_text(render_sheet(rows, result_file), encoding="utf-8")
    logger.info(
        "%s 판정 %d건 (impact %d / dept %d), 케이스 %d개, 실패 %d건, 시드 %d",
        TARGET_LEVEL,
        len(rows),
        impact_rows,
        dept_rows,
        len(cases_covered),
        len(failures),
        SHUFFLE_SEED,
    )
    logger.info("판정지: %s", out_sheet)
    logger.info("결과: %s", out_json)


def run_score(out_json: Path, out_sheet: Path) -> None:
    """채워진 판정지를 읽어 F-6 을 계산하고 결과 JSON 에 되쓴다."""
    payload: dict[str, Any] = json.loads(out_json.read_text(encoding="utf-8"))
    verdicts = read_verdicts(out_sheet, int(payload["sheet_rows"]))
    for row in payload["rows"]:
        row["human_level"] = verdicts[int(row["sheet_row"])]
    payload["score"] = score(payload, verdicts)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = payload["score"]
    logger.info("사람 판정 분포: %s", summary["human_tally"])
    logger.info(
        "F-6 = %s (95%% 구간 %s), 불일치율 %s, 일치율 %s",
        summary["f6"],
        summary["f6_ci95"],
        summary["mismatch_rate"],
        summary["agreement_rate"],
    )
    for entry in summary["disagreements"]:
        logger.info(
            "  %s행 %s %s → 사람 %s",
            entry["sheet_row"],
            entry["case_id"],
            entry["key"],
            entry["human_level"],
        )
    logger.info("결과: %s", out_json)


def main() -> None:
    """진입점."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_dotenv()
    parser = argparse.ArgumentParser(description="인용 적합도 판정지 (docs/23 §2)")
    parser.add_argument(
        "--result",
        type=Path,
        default=RESULTS_DIR / "impact-claude-sonnet-5-20260825T214746Z.json",
    )
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "citation-adequacy.json")
    parser.add_argument(
        "--sheet", type=Path, default=REPO_ROOT / "docs" / "23-citation-adequacy-sheet.md"
    )
    parser.add_argument(
        "--score",
        action="store_true",
        help="채워진 판정지를 읽어 F-6 과 신뢰구간을 계산한다 (판정지를 다시 만들지 않는다)",
    )
    args = parser.parse_args()
    if args.score:
        run_score(args.out, args.sheet)
    else:
        run(args.result, args.out, args.sheet)


if __name__ == "__main__":
    main()
