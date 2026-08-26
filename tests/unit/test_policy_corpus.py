"""사내 정책 문서 5종이 골든셋 시나리오와 코퍼스 설계를 만족하는지 검사한다.

이 테스트가 존재하는 이유: 골든셋은 **문서를 가리키는 포인터**다
(`doc_id` + `article_spec`). 문서를 고치다 조 번호나 제목이 어긋나면 러너는 실패하지 않고
"그 조항을 찾지 못했다"는 낮은 점수를 낸다 — 지표가 나쁜 것과 참조가 깨진 것을 구별할
수 없게 된다. 참조 무결성은 채점 이전에 기계로 잡아야 한다.

수치·주체·인용까지 검사하는 이유: 난이도는 문서의 성질에서 나온다
(`docs/09-corpus-design.md` §5). 예컨대 ISP-PROC-002 제7조의 "즉시"를 누군가
"24시간 이내"로 고치면 case-001은 여전히 통과하는 것처럼 보이지만 실제로는 EASY 기준선이
사라진다. 그 사라짐은 지표에 드러나지 않는다.

EMPTY 영역 검사가 가장 중요하다: 다섯 문서 중 어느 하나에 해당 영역의 조항이 들어오면
EMPTY 시나리오 3건이 전부 무효가 되는데, 그때도 테스트가 없으면 아무도 알아채지 못한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from regchange.retrieval.corpus import ARTICLE_SPEC, load_corpus
from regchange.retrieval.models import PolicyDocument

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "evals" / "corpus" / "internal-policies"
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"

# 파서는 `src/regchange/retrieval/corpus.py` 로 승격했다. 여기서 다시 정의하지 않는다 —
# 채점이 보는 조 경계와 검색이 보는 조 경계가 다른 코드로 정해지면, 어긋났을 때
# "검색이 못 찾았다"는 낮은 점수로만 나타나고 원인은 드러나지 않는다.

DOC_ARTICLE_COUNTS = {
    "ISP-POL-001": 18,
    "ISP-GUIDE-002": 44,
    "ISP-GUIDE-003": 40,
    "ISP-PROC-001": 27,
    "ISP-PROC-002": 23,
}
"""`docs/09-corpus-design.md` §3의 목차. `tests/unit/test_golden_dataset.py` 와 같은 값을
의도적으로 중복해 둔다 — 한쪽은 시나리오가 문서 크기를 넘는지, 다른 한쪽은 문서가 실제로
그 크기인지를 보므로 검사 대상이 다르다."""

METADATA_REQUIRED = (
    "doc_id",
    "title",
    "version",
    "effective_date",
    "owner_dept",
    "classification",
    "parent_laws",
    "revision_history",
)

MIN_REVISIONS = 3
"""`docs/09-corpus-design.md` §6 — 실제 은행 규정은 앞머리에 개정 이력표를 두며,
정책 문서의 bitemporal 처리(원칙 6)를 시험할 재료가 된다."""

METADATA_DEADLINE = "2026-02-01"
"""모든 문서의 `effective_date` 상한. 골든셋의 가장 이른 시행일이 2026-08-13(case-013)이므로
문서가 그보다 나중이면 "개정을 이미 반영한 문서"가 되어 시나리오가 성립하지 않는다.
2026-02-01 은 그 사이에 둔 여유 있는 경계다 (`docs/09-corpus-design.md` §6)."""


DOCUMENTS: dict[str, PolicyDocument] = {doc.doc_id: doc for doc in load_corpus(CORPUS_DIR)}

CASES: list[tuple[str, dict[str, Any]]] = [
    (path.name, yaml.safe_load(path.read_text(encoding="utf-8")))
    for path in sorted(GOLDEN_DIR.glob("case-*.yaml"))
]


def golden_references() -> list[tuple[str, str, str, str]]:
    """골든셋이 가리키는 (케이스, 역할, doc_id, article_spec) 을 전부 모은다."""
    refs: list[tuple[str, str, str, str]] = []
    for _, case in CASES:
        for impact in case.get("expected_impacts") or []:
            refs.append((case["id"], "IMPACT", impact["doc_id"], str(impact["article_spec"])))
        for decoy in case.get("decoys") or []:
            refs.append((case["id"], "DECOY", decoy["doc_id"], str(decoy["article_spec"])))
    return refs


REFERENCES = golden_references()


# ---------------------------------------------------------------------------
# 문서 자체의 형식
# ---------------------------------------------------------------------------


def test_all_five_documents_exist() -> None:
    """문서 5종이 모두 있다. 하나가 빠지면 그 문서를 가리키는 시나리오가 통째로 죽는다."""
    assert set(DOCUMENTS) == set(DOC_ARTICLE_COUNTS), f"문서 목록: {sorted(DOCUMENTS)}"


@pytest.mark.parametrize("doc_id", sorted(DOC_ARTICLE_COUNTS))
def test_article_numbering_is_complete(doc_id: str) -> None:
    """조 번호가 1부터 목차의 조문 수까지 빠짐없이 이어진다."""
    document = DOCUMENTS[doc_id]
    expected = DOC_ARTICLE_COUNTS[doc_id]
    assert sorted(document.by_article_no) == list(range(1, expected + 1)), (
        f"{doc_id}: 조 번호가 1~{expected} 와 다르다"
    )


@pytest.mark.parametrize("doc_id", sorted(DOC_ARTICLE_COUNTS))
def test_metadata_is_present(doc_id: str) -> None:
    """메타데이터 필수 항목과 개정 이력 최소 건수를 검사한다 (`09-corpus-design.md` §6).

    필수 항목의 존재 자체는 파서가 `CorpusError` 로 막지만(`METADATA_REQUIRED`),
    여기서 원본 front matter 를 다시 읽어 확인한다 — 파서가 기본값을 넣기 시작하면
    문서에 없는 값이 있는 것처럼 보이게 되고, 그 변화가 이 테스트에 걸려야 한다.
    """
    document = DOCUMENTS[doc_id]
    raw = (CORPUS_DIR / document.source_path).read_text(encoding="utf-8")
    front_matter: dict[str, Any] = yaml.safe_load(raw.split("---\n", 2)[1])
    for field in METADATA_REQUIRED:
        assert field in front_matter, f"{doc_id}: `{field}` 없음"

    assert document.doc_id == doc_id
    assert len(document.revision_history) >= MIN_REVISIONS, f"{doc_id}: 개정 이력 부족"
    for entry in document.revision_history:
        assert {"version", "date", "summary"} <= set(entry), f"{doc_id}: 개정 이력 항목 누락"


@pytest.mark.parametrize("doc_id", sorted(DOC_ARTICLE_COUNTS))
def test_effective_date_precedes_scenarios(doc_id: str) -> None:
    """`effective_date` 가 골든셋의 시행일보다 앞선다.

    문서가 개정 이후 시점이면 "개정을 이미 반영한 문서"가 되어 시나리오가 성립하지 않는다.
    """
    effective = DOCUMENTS[doc_id].effective_date.isoformat()
    assert effective < METADATA_DEADLINE, f"{doc_id}: effective_date {effective}"


@pytest.mark.parametrize("doc_id", sorted(DOC_ARTICLE_COUNTS))
def test_articles_are_not_empty(doc_id: str) -> None:
    """본문이 빈 조항이 없다. 검색 대상이 없는 조항은 참조만 성립하고 채점되지 않는다."""
    for article in DOCUMENTS[doc_id].articles:
        assert article.text_raw.strip(), f"{doc_id} 제{article.article_no}조: 본문이 비어 있다"


# ---------------------------------------------------------------------------
# 골든셋 참조 무결성
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case_id", "role", "doc_id", "spec"), REFERENCES)
def test_golden_reference_resolves(case_id: str, role: str, doc_id: str, spec: str) -> None:
    """골든셋이 가리키는 조항이 실제 문서에 같은 번호·같은 제목으로 있다."""
    match = ARTICLE_SPEC.match(spec)
    assert match is not None, f"{case_id}: article_spec 형식이 아니다: {spec!r}"

    number, title = int(match.group(1)), match.group(2).strip()
    document = DOCUMENTS[doc_id]
    assert number in document.by_article_no, f"{case_id} {role}: {doc_id} 제{number}조가 없다"
    assert document.by_article_no[number].article_title == title, (
        f"{case_id} {role}: {doc_id} 제{number}조 제목이 다르다 — "
        f"문서={document.by_article_no[number].article_title!r} 골든셋={title!r}"
    )


# ---------------------------------------------------------------------------
# EMPTY 시나리오 — 비어 있어야 하는 영역
# ---------------------------------------------------------------------------

EMPTY_AREAS = {
    "case-013 외부기관 자료 제공 요청": ("채무조정", "채권의 매입", "자료 제공을 요청받"),
    "case-014 불법정보·허위조작정보": ("불법정보", "허위조작", "게시물", "게시판", "투명성센터"),
    "case-015 이용자 자금 보호": ("선불충전금", "정산대상금액", "별도관리", "신탁", "예치금"),
}
"""다섯 문서 어디에도 나오면 안 되는 어휘. `docs/09-corpus-design.md` §4.

어휘로 대리 검사하는 이유: "그 영역을 다루는 조항이 없다"는 것은 기계로 증명할 수 없다.
그 영역에서만 쓰이는 표현이 문서에 등장하지 않는다는 것이 확인 가능한 최선의 대리 지표다.
사람 검토(`docs/09-corpus-design.md` §4 대조표)를 대체하지 않는다."""


@pytest.mark.parametrize(("area", "forbidden"), sorted(EMPTY_AREAS.items()))
def test_empty_areas_stay_empty(area: str, forbidden: tuple[str, ...]) -> None:
    """EMPTY 시나리오가 요구하는 공백이 유지된다.

    이 셋 중 하나라도 문서에 들어오면 "모른다고 말하는 기능"의 측정이 사라진다.
    """
    for doc_id, document in DOCUMENTS.items():
        body = (CORPUS_DIR / document.source_path).read_text(encoding="utf-8")
        hits = [word for word in forbidden if word in body]
        assert not hits, f"{area}: {doc_id} 에 {hits} 가 있다"


# ---------------------------------------------------------------------------
# 난이도를 만드는 성질 — 수치 · 주체 · 인용
# ---------------------------------------------------------------------------

REQUIRED_PHRASES = [
    # (doc_id, 조, 반드시 들어 있어야 할 문구, 근거)
    ("ISP-PROC-002", 7, "즉시", "§5.1 신고 시점. 법령이 24시간으로 바뀌는 것이 case-001의 논점"),
    ("ISP-PROC-002", 7, "제48조의3", "§5.3 명시적 인용 — case-001 EASY 기준선"),
    ("ISP-GUIDE-002", 35, "2시간", "§5.1 사내 보고 시한. 법정 24시간과의 정합성 검토(case-001)"),
    ("ISP-PROC-001", 18, "2년", "§5.1 접속기록 보관. 수치가 있다는 이유로 끌려오는 함정"),
    ("ISP-GUIDE-002", 20, "매년 1회", "§5.1 사후심사 주기"),
    ("ISP-GUIDE-002", 20, "서면", "§5.1 서면 제출만 — 현장심사 병행 요구가 결손을 드러낸다"),
    ("ISP-GUIDE-002", 8, "분기 1회", "§5.1 협의회 개최 주기"),
    ("ISP-GUIDE-002", 8, "정보보호부장", "§5.2 위원장. 법령은 정보보호 최고책임자를 요구한다"),
    ("ISP-GUIDE-002", 8, "협의", "§7.4 어휘 지정 — 심의가 아니라 협의·조정 기구"),
    ("ISP-GUIDE-002", 38, "반기 1회", "§5.1 자체점검 주기"),
    ("ISP-GUIDE-002", 38, "자체점검", "§7.4 어휘 지정 — 법령의 '평가'와 구분된다"),
    ("ISP-PROC-001", 14, "반기 1회", "§5.1 권한 검토 주기. 주기 어휘가 겹치는 함정"),
    ("ISP-PROC-002", 19, "연 1회 이상", "§5.1 대응 훈련 주기"),
    ("ISP-GUIDE-003", 12, "3년", "§5.1 처리 기록 보존기간"),
    ("ISP-GUIDE-003", 12, "제20조", "§5.3 신용정보법 인용 — case-011·013의 함정을 성립시킨다"),
    ("ISP-GUIDE-003", 5, "대표이사", "§5.2 결재선. 법령은 이사회 의결을 요구한다"),
    ("ISP-GUIDE-003", 5, "임원", "case-002 decoy — 어휘 중복이 가장 심한 함정"),
    ("ISP-GUIDE-002", 11, "예산 편성", "case-002 decoy — 추가된 마목과 문자 그대로 겹쳐야 한다"),
    ("ISP-GUIDE-002", 11, "비율", "§5.1 예산 '규모 기준'이라는 성격이 why_not 의 근거다"),
    ("ISP-POL-001", 5, "경영진", "§5.2 귀속 주체를 특정하지 않는다"),
    ("ISP-POL-001", 8, "운영할 수 있다", "§7.3 재량 규정. 법령이 의무화하는 것이 case-005"),
    ("ISP-PROC-002", 8, "홍보부", "§5.2 재량 규정. 법령은 의무로 바뀐다"),
    ("ISP-PROC-002", 8, "고객 안내", "§7.4 어휘 지정 — 법령의 '이용자 통지'와 다르게 쓴다"),
    (
        "ISP-PROC-002",
        4,
        "「정보통신망 이용촉진 및 정보보호 등에 관한 법률」",
        "case-014 decoy — 법령명 경로로 끌려오는 함정. 조 번호는 넣지 않는다(아래 주석)",
    ),
    ("ISP-GUIDE-002", 7, "제45조의3제4항", "§5.3 명시적 인용 — case-002 EASY, case-003 함정"),
    ("ISP-GUIDE-002", 19, "제47조", "§5.3 명시적 인용 — case-004 EASY"),
    ("ISP-GUIDE-003", 6, "제31조제3항", "§5.3 개정 전 항 번호. 개정 후 제4항으로 밀린다"),
    ("ISP-GUIDE-003", 17, "제29조", "§5.3 명시적 인용 — case-009"),
    ("ISP-GUIDE-003", 26, "제34조제3항", "§5.3 개정 전 항 번호. 개정 후 제4항으로 밀린다"),
    ("ISP-GUIDE-003", 28, "제17조", "§5.3 명시적 인용 — case-013의 가장 그럴듯한 함정"),
    ("ISP-GUIDE-003", 28, "제18조", "§5.3 제44조의4가 적용을 배제하는 조문"),
    ("ISP-PROC-002", 11, "제34조제1항", "§5.3 명시적 인용 — case-008 EASY"),
    ("ISP-POL-001", 2, "전자금융거래법", "§4 이 포함이 case-015의 함정을 성립시킨다"),
]


@pytest.mark.parametrize(("doc_id", "number", "phrase", "why"), REQUIRED_PHRASES)
def test_required_phrase_present(doc_id: str, number: int, phrase: str, why: str) -> None:
    """난이도를 만드는 수치·주체·인용이 지정된 조항에 그대로 있다."""
    assert phrase in DOCUMENTS[doc_id].text_of(number), (
        f"{doc_id} 제{number}조에 {phrase!r} 없음 — {why}"
    )


AMENDED_LAW_ARTICLES = {
    # 골든셋 8개 원천이 실제로 건드린 법령 조문. 스냅샷의 `<개정 …>`/`<신설 …>` 표기를
    # 전수로 뽑아 확인했다 (개보법 2026.3.10 18건, 정통망법 2026.3.31 25건 / 2026.1.6 26건,
    # 전금법 2025.12.16 15건).
    #
    # 여기 있는 조문을 시나리오가 지정하지 않은 사내 조항이 인용하면, 그 조항은 함정이 아니라
    # **진짜 영향받는 조항**이 된다. 골든셋에 없는 정답이 생기면 재현율이 실제보다 낮게 나온다.
    ("ISP-PROC-002", 4, "제2조제1항제7호"): "정보통신망법 제2조는 case-014 원천이 개정했다",
    (
        "ISP-GUIDE-003",
        14,
        "「개인정보 보호법」 제26조",
    ): "개보법 제26조는 case-007~010 원천이 개정했다",
    ("ISP-GUIDE-003", 21, "「개인정보 보호법」 제25조"): "개보법 제25조는 같은 원천이 개정했다",
}


@pytest.mark.parametrize(("key", "why"), sorted(AMENDED_LAW_ARTICLES.items()))
def test_unlisted_articles_do_not_cite_amended_law_articles(
    key: tuple[str, int, str], why: str
) -> None:
    """시나리오가 지정하지 않은 조항이 개정 대상 법령 조문을 인용하지 않는다.

    문서를 그럴듯하게 쓰다 보면 자연스러운 인용을 넣게 되는데, 그 인용 대상이 마침
    골든셋 원천이 개정한 조문이면 함정이 정답으로 바뀐다. 실제로 초안에서 두 건
    (ISP-GUIDE-003 제14조의 개보법 제26조, ISP-PROC-002 제4조의 정통망법 제2조)이
    이 경로로 들어왔다가 여기서 걸렸다.
    """
    doc_id, number, citation = key
    assert citation not in DOCUMENTS[doc_id].text_of(number), f"{doc_id} 제{number}조 — {why}"


FORBIDDEN_PHRASES = [
    # (doc_id, 조, 들어 있으면 안 되는 문구, 근거)
    ("ISP-GUIDE-002", 7, "예산", "case-002 — 추가된 마목(인력·예산)이 결손이어야 한다"),
    ("ISP-GUIDE-002", 7, "이사회", "case-002 — 추가된 바목(이사회 보고)이 결손이어야 한다"),
    ("ISP-GUIDE-002", 20, "현장", "case-004 — 현장심사 수검 절차가 결손이어야 한다"),
    ("ISP-GUIDE-003", 5, "이사회", "case-007 — 이사회 의결이 결손이어야 한다"),
    ("ISP-GUIDE-003", 5, "보호위원회", "case-007 — 보호위원회 신고가 결손이어야 한다"),
    ("ISP-GUIDE-003", 26, "회수", "case-008 — ③에 추가된 회수·삭제가 결손이어야 한다"),
    ("ISP-GUIDE-003", 31, "대리인", "case-012 — 대리인 경유 요구의 처리 기준이 결손이어야 한다"),
    ("ISP-GUIDE-002", 31, "대리인", "case-012 — 자동화 도구와의 협의 방식이 결손이어야 한다"),
    ("ISP-POL-001", 5, "최종", "case-010 — '최종 책임'이라는 표현이 없어야 한다"),
    ("ISP-POL-001", 5, "예산", "case-010 — 인력·예산을 명시한 부분이 없어야 한다"),
    ("ISP-POL-001", 6, "임원", "case-002 — 임원인지 직원인지 명시하지 않는다"),
    ("ISP-PROC-002", 4, "매뉴얼", "case-006 — 문서를 '절차서'라고만 부르는 것이 요점이다"),
    ("ISP-PROC-002", 19, "제출", "case-006 — 갱신본의 대외 제출이 결손이어야 한다"),
    ("ISP-GUIDE-002", 38, "공개", "case-005 — 대외 제출·공개가 결손이어야 한다"),
]


@pytest.mark.parametrize(("doc_id", "number", "phrase", "why"), FORBIDDEN_PHRASES)
def test_forbidden_phrase_absent(doc_id: str, number: int, phrase: str, why: str) -> None:
    """개정이 드러내야 할 결손이 실수로 메워지지 않았다.

    이 검사가 없으면 문서를 "그럴듯하게" 다듬는 과정에서 결손이 채워지고,
    그때 시나리오는 IMPACT 를 잃지만 테스트는 통과한다.
    """
    assert phrase not in DOCUMENTS[doc_id].text_of(number), (
        f"{doc_id} 제{number}조에 {phrase!r} 가 있다 — {why}"
    )


NO_CITATION_ARTICLES = [
    ("ISP-GUIDE-002", 8),
    ("ISP-GUIDE-002", 20),
    ("ISP-GUIDE-002", 35),
    ("ISP-PROC-002", 8),
    ("ISP-POL-001", 5),
    ("ISP-POL-001", 7),
    ("ISP-PROC-001", 24),
    ("ISP-GUIDE-003", 31),
]
"""`docs/09-corpus-design.md` §5.3 의 "인용 없음" 목록.

제26조(개인정보 유출 시 조치)는 여기 없다. §5.3 의 두 표가 서로 다르게 지정하고 있었고,
case-008 의 `content_spec`(「개인정보 보호법」 제34조제3항 인용)이 더 구체적이므로 그쪽을
따랐다. §5.3 은 2026-08-20 에 정정됐다 — `docs/09-corpus-verification.md` §3-1."""

LAW_CITATION = re.compile(r"「[^」]+」\s*제\d+조")


@pytest.mark.parametrize(("doc_id", "number"), NO_CITATION_ARTICLES)
def test_no_citation_articles_have_none(doc_id: str, number: int) -> None:
    """인용이 없어야 MEDIUM 인 조항에 조 단위 법령 인용이 들어오지 않았다.

    인용이 있으면 매칭이 쉬워져 MEDIUM 이 EASY 가 되고 난이도 설계가 무너진다.
    """
    text = DOCUMENTS[doc_id].text_of(number)
    assert not LAW_CITATION.search(text), f"{doc_id} 제{number}조에 법령 인용이 있다"


@pytest.mark.parametrize("doc_id", sorted(DOC_ARTICLE_COUNTS))
def test_no_unresolved_verify_markers(doc_id: str) -> None:
    """확인하지 못한 인용을 남긴 채 두지 않았다 (CLAUDE.md §5.1).

    `TODO(verify)` 가 남아 있는 것 자체는 정상이지만, 이 코퍼스는 검색 평가의 대상이므로
    미확인 자리가 조항 본문에 남으면 그 조항이 무엇을 인용하는지 채점할 수 없다.
    """
    body = (CORPUS_DIR / DOCUMENTS[doc_id].source_path).read_text(encoding="utf-8")
    assert "TODO(verify)" not in body, f"{doc_id}: 미해소 TODO(verify) 가 있다"
