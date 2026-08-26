"""질의 구성이 규약(`docs/10-retrieval-evaluation-protocol.md` §2)대로 고정돼 있는지 검사한다.

이 테스트가 존재하는 이유: 질의 구성이 바뀌면 지표가 통째로 움직인다. 측정 스크립트가
자기 방식으로 질의를 만들면 운영 경로와 다른 것을 재게 되고, 그 차이는 지표에 드러나지
않는다. 규약 변경은 이 테스트를 깨뜨려야 한다.
"""

from __future__ import annotations

import pytest

from regchange.retrieval.query import build_query


def test_query_carries_law_name_and_path() -> None:
    """법령명과 조문 경로가 앞에 붙는다 — 사내 문서의 명시적 인용과 겹치는 자리다."""
    query = build_query(
        law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
        article_path="제48조의3",
        after_text="침해사고 발생 시 24시간 이내에 신고하여야 한다.",
    )
    assert query.startswith("정보통신망 이용촉진 및 정보보호 등에 관한 법률 제48조의3")
    assert "24시간" in query


def test_query_works_without_article_path() -> None:
    """조문 경로가 없어도 질의가 성립한다. 식별자 매칭에만 기여하는 값이다."""
    query = build_query(law_name="전자금융거래법", article_path=None, after_text="본문")
    assert query == "전자금융거래법\n본문"


def test_empty_after_text_fails() -> None:
    """개정 후 본문이 비면 실패한다. 법령명만으로 검색하면 그 문서의 아무 조항이나 올라온다."""
    with pytest.raises(ValueError, match="비어 있어"):
        build_query(law_name="전자금융거래법", article_path="제21조", after_text="   ")
