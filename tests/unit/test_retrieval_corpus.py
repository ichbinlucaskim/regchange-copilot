"""승격된 코퍼스 파서가 조 경계와 실패를 계약대로 다루는지 검사한다.

이 테스트가 존재하는 이유: 이 파서는 `tests/unit/test_policy_corpus.py` 안에만 있던
코드였다. 승격했으므로 이제 운영 경로이며, 실패 시 부분 결과를 만들지 않는다는 계약이
테스트로 고정돼야 한다. 조용한 부분 파싱은 "검색이 못 찾았다"로만 나타난다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regchange.retrieval.corpus import (
    CorpusError,
    load_corpus,
    parse_article_spec,
    parse_policy_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "evals" / "corpus" / "internal-policies"

FRONT_MATTER = """---
doc_id: ISP-TEST-001
title: 시험 문서
version: "1.0"
effective_date: 2025-01-01
owner_dept: 정보보호부
classification: INTERNAL
parent_laws:
  - 전자금융거래법
revision_history:
  - {version: "1.0", date: 2025-01-01, summary: "제정"}
---

# 시험 문서

### 제1조(목적)
이 문서는 시험용이다.
"""


def _write(tmp_path: Path, body: str, name: str = "ISP-TEST-001.md") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_real_corpus_parses_to_152_articles() -> None:
    """실제 코퍼스 5종 152조가 그대로 나온다. 검색 대상 규모의 기준선이다."""
    documents = load_corpus(CORPUS_DIR)
    assert len(documents) == 5
    assert sum(len(document.articles) for document in documents) == 152


def test_article_carries_raw_and_norm() -> None:
    """인용은 `text_raw`, 색인은 `text_norm` 을 본다 (ADR-002). 둘 다 채워져야 한다."""
    document = next(d for d in load_corpus(CORPUS_DIR) if d.doc_id == "ISP-PROC-002")
    article = document.by_article_no[7]
    assert "제48조의3" in article.text_raw
    assert "제48조의3" in article.text_norm
    assert "\n" not in article.text_norm, "정규화본은 공백이 접혀 있어야 한다"
    assert article.text_norm_sha256


def test_article_title_is_indexed() -> None:
    """조 제목이 색인 입력에 포함된다. 빼면 조의 핵심 어휘가 색인에서 사라진다."""
    document = next(d for d in load_corpus(CORPUS_DIR) if d.doc_id == "ISP-GUIDE-002")
    assert "침해사고 예방점검" in document.by_article_no[35].text_norm


def test_spec_matches_golden_notation() -> None:
    """`PolicyArticle.spec` 이 골든셋 `article_spec` 과 같은 표기를 만든다."""
    document = next(d for d in load_corpus(CORPUS_DIR) if d.doc_id == "ISP-PROC-001")
    assert document.by_article_no[18].spec == "제18조 (접속기록의 보관)"


def test_missing_front_matter_fails(tmp_path: Path) -> None:
    """front matter 가 없으면 실패한다. 메타데이터 없는 문서는 코퍼스가 아니다."""
    with pytest.raises(CorpusError):
        parse_policy_document(_write(tmp_path, "### 제1조(목적)\n본문\n"))


def test_missing_metadata_field_fails(tmp_path: Path) -> None:
    """필수 메타데이터가 빠지면 실패한다. `effective_date` 가 없으면 시점 검색이 조용히 틀린다."""
    broken = FRONT_MATTER.replace("effective_date: 2025-01-01\n", "")
    with pytest.raises(CorpusError, match="메타데이터 누락"):
        parse_policy_document(_write(tmp_path, broken))


def test_duplicate_article_number_fails(tmp_path: Path) -> None:
    """조 번호가 겹치면 실패한다. 뒤의 것이 앞의 것을 덮어써 조용히 사라지는 것을 막는다."""
    body = FRONT_MATTER + "\n### 제1조(중복)\n다른 본문\n"
    with pytest.raises(CorpusError, match="두 번"):
        parse_policy_document(_write(tmp_path, body))


def test_empty_article_body_fails(tmp_path: Path) -> None:
    """본문이 빈 조는 실패한다. 참조만 성립하고 채점되지 않는 결손을 적재 전에 막는다."""
    body = FRONT_MATTER + "\n### 제2조(빈 조)\n\n"
    with pytest.raises(CorpusError, match="본문이 비어"):
        parse_policy_document(_write(tmp_path, body))


def test_no_articles_fails(tmp_path: Path) -> None:
    """조가 하나도 없으면 실패한다. 파싱 실패를 빈 결과로 숨기지 않는다."""
    head = FRONT_MATTER.split("### 제1조")[0]
    with pytest.raises(CorpusError, match="조를 하나도"):
        parse_policy_document(_write(tmp_path, head))


def test_empty_directory_fails(tmp_path: Path) -> None:
    """경로가 틀렸을 때 '코퍼스가 비었음'으로 보이면 측정이 전부 0점으로 나온다."""
    with pytest.raises(CorpusError):
        load_corpus(tmp_path)


def test_parse_article_spec_rejects_bad_format() -> None:
    """골든셋 표기가 어긋나면 조용한 채점 실패 대신 예외를 낸다."""
    assert parse_article_spec("제18조 (접속기록의 보관)") == (18, "접속기록의 보관")
    with pytest.raises(CorpusError):
        parse_article_spec("18조 접속기록")
