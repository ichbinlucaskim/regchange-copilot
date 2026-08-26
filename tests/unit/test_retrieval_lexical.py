"""문자 bigram 토큰화와 BM25 가 조사 변화를 넘어서는지 검사한다.

이 테스트가 존재하는 이유: 어휘 검색은 하이브리드 결합의 한 축이고, 그 축이 약하면
"하이브리드가 이득인가"라는 측정 질문에 허수아비를 세우고 답하게 된다. 한국어에서
어절 단위 색인이 실패하는 지점(조사·어미 변화)과, bigram 이 그것을 넘는다는 사실을
고정해 둔다.
"""

from __future__ import annotations

from regchange.retrieval.lexical import bigrams, build_index, rank_only, search


def test_bigrams_split_within_token_only() -> None:
    """토큰 경계를 넘는 bigram 을 만들지 않는다. `조침` 같은 무의미한 겹침을 막는다."""
    tokens = bigrams("제7조 침해사고")
    assert "조침" not in tokens
    assert "침해" in tokens
    assert "해사" in tokens


def test_bigrams_survive_particle_change() -> None:
    """`침해사고를` 와 `침해사고가` 가 대부분의 bigram 을 공유한다 — 어절 색인이 실패하는 지점."""
    left, right = set(bigrams("침해사고를")), set(bigrams("침해사고가"))
    shared = left & right
    assert {"침해", "해사", "사고"} <= shared


def test_bigrams_keep_article_identifier() -> None:
    """`제48조의3` 같은 식별자가 정확히 겹친다. 형태소 분석기가 오히려 잘못 쪼개는 부분이다."""
    tokens = set(bigrams("법 제48조의3에 따라"))
    assert {"제4", "48", "8조", "조의", "의3"} <= tokens


def test_single_character_token_survives() -> None:
    """한 글자 토큰은 버리지 않는다. 버리면 `제5조 및 제6조` 의 구조가 사라진다."""
    assert "및" in bigrams("제5조 및 제6조")


def test_bm25_prefers_lexically_closer_article() -> None:
    """법령 어휘를 그대로 쓴 조가, 같은 주제를 다른 어휘로 쓴 조보다 위에 온다."""
    index = build_index(
        [
            ("A", "침해사고 신고 정보통신서비스 제공자는 침해사고를 즉시 신고한다"),
            ("B", "고객 안내 보안사고 발생 시 홍보부와 협의하여 고객에게 안내한다"),
            ("C", "접속기록의 보관 접속기록을 2년간 보관한다"),
        ]
    )
    ranked = rank_only(search(index, "침해사고가 발생하면 즉시 신고하여야 한다", limit=3))
    assert ranked[0] == "A"


def test_bm25_returns_nothing_when_no_overlap() -> None:
    """겹치는 bigram 이 없으면 빈 결과다. 0점 문단을 순위에 올리면 '아무거나 고른 것'이 된다."""
    index = build_index([("A", "접속기록을 2년간 보관한다")])
    assert search(index, "zzzz", limit=5) == ()


def test_bm25_is_deterministic_on_ties() -> None:
    """동점은 식별자 순으로 안정 정렬한다. 실행마다 순위가 흔들리면 재현이 깨진다."""
    documents = [("B", "정보보호 관리체계"), ("A", "정보보호 관리체계")]
    first = rank_only(search(build_index(documents), "정보보호 관리체계", limit=2))
    second = rank_only(search(build_index(reversed(documents)), "정보보호 관리체계", limit=2))
    assert first == second == ("A", "B")


def test_empty_index_returns_nothing() -> None:
    """빈 색인에 검색해도 예외가 아니라 빈 결과다."""
    assert search(build_index([]), "무엇이든", limit=5) == ()
