"""문자 bigram 색인과 BM25 — 한국어 어휘 검색을 형태소 분석기 없이 세운다.

목적:
    사내 규정 문단과 질의를 문자 2-gram 으로 쪼개고, BM25 로 어휘 유사도 순위를 낸다.
    벡터 검색이 놓치는 정확 일치(법령 인용, 수치, 고유 용어)를 잡는 축이다.

구현 이유:
    **어절 단위 색인을 쓰지 않는다.** 한국어는 조사가 붙어 `침해사고를` 와 `침해사고가`
    가 다른 토큰이 된다. 사내 규정과 법령 원문은 같은 개념을 다른 조사로 쓰므로 어절
    일치는 대부분 빗나간다. 문자 2-gram 은 `침해`·`해사`·`사고` 세 개를 공유하므로
    조사 차이를 넘는다. `제48조의3` 같은 식별자도 `제4`·`48`·`8조`·`조의`·`의3` 로
    정확히 겹친다 — 형태소 분석기가 오히려 잘못 쪼개는 부분이다.

    **Postgres 전문검색(`ts_rank`)을 쓰지 않고 BM25 를 직접 구현한다.** Postgres 에는
    한국어 형태소 분석기가 없고, `simple` 설정 위에 bigram 을 얹어도 `ts_rank` 에는
    **IDF 가 없다.** IDF 없이 순위를 매기면 `하여야`·`정보보` 처럼 모든 문서에 있는
    bigram 이 점수를 지배하고, 어휘 검색이 실제보다 나빠 보인다. 그렇게 재면 "하이브리드가
    이득인가"라는 이번 측정의 질문에 허수아비를 세우고 답하는 셈이 된다.

    **BM25 계수를 우리 코퍼스로 튜닝하지 않는다.** `k1=1.2`, `b=0.75` 는 문헌 표준값
    그대로다. 15건으로 계수를 맞추면 그 15건에 과적합되고, 과적합된 어휘 검색이
    하이브리드 비교를 오염시킨다. 튜닝은 골든셋이 50~80건이 되는 6단계의 일이다.

트레이드오프:
    - **점수 계산이 DB 가 아니라 애플리케이션에서 일어난다.** `as_of` 로 걸러진 문단
      전체를 메모리로 읽어 색인을 만든다. 152 문단(실제로는 수천 조 규모까지)에서는
      밀리초이지만, 수십만 문단이 되면 성립하지 않는다. **한계 신호**: 코퍼스가 커져
      색인 구축이 검색 지연을 지배할 때. 그때는 `pg_search` 계열 확장이나 별도 검색
      엔진으로 옮기며, 그 이전에 옮기면 검증할 수 없는 복잡도를 먼저 지불하는 것이다.
    - 2-gram 은 3글자 이상의 고유명사에서 부분 일치를 만든다. `정보보호` 와 `정보통신`
      이 `정보` 를 공유한다. IDF 가 흔한 bigram 의 가중치를 낮춰 완화하지만 없애지는
      못한다. 그 대신 조사·어미 변화에 강해진다.
    - 색인이 불변 자료구조라 문단이 추가되면 통째로 다시 만든다. 증분 갱신을 포기한
      대신 "색인과 문단 집합이 어긋난 상태"가 존재할 수 없다.

엣지 케이스:
    - 빈 문자열: 빈 토큰 목록. 예외를 던지지 않는다 — 빈 질의는 상위 계층이 막는다.
    - 한 글자 토큰(`및`, `등`): 2-gram 을 만들 수 없으므로 그 글자 자체를 토큰으로 쓴다.
      버리면 `제5조 및 제6조` 의 구조가 사라진다.
    - 질의에만 있고 어느 문단에도 없는 bigram: `df=0` 이므로 점수에 기여하지 않는다.
      0 나눗셈이 나지 않도록 IDF 식이 `df+0.5` 를 쓴다.
    - 문단이 하나도 없는 색인: 검색은 빈 결과를 반환한다. 유사도 임계값을 낮춰 억지로
      채우지 않는다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

BM25_K1 = 1.2
"""용어 빈도 포화 계수. Robertson 계열 문헌의 표준값이며 우리 코퍼스로 튜닝하지 않았다.

값을 키우면 같은 bigram 이 여러 번 나온 문단이 더 유리해진다. 조 단위 문단은 길이가
고르므로(중앙값 148자) 이 계수의 영향이 크지 않다."""

BM25_B = 0.75
"""문서 길이 정규화 계수. 표준값. 0이면 길이를 무시하고 1이면 완전 정규화한다.

조 길이가 148~486자로 3배 차이 나므로 길이 정규화를 끄면 긴 조가 유리해진다."""

NGRAM_SIZE = 2
"""문자 n-gram 크기. 한국어 형태소가 대개 2음절이며, 3-gram 은 조사 변화에 다시 취약해진다."""

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ]+")
"""토큰 경계. 공백·구두점·괄호·낫표(「」)를 전부 경계로 본다.

경계를 넘어 bigram 을 만들지 않는 이유: `제7조 침해사고` 에서 `조침` 이라는, 어느
문서에도 의미가 없는 bigram 이 생기고 그것이 우연히 겹치면 잡음이 된다."""


def bigrams(text: str) -> tuple[str, ...]:
    """텍스트를 문자 2-gram 토큰으로 쪼갠다.

    목적:
        질의와 문단을 같은 규칙으로 토큰화해 어휘 겹침을 셀 수 있게 만든다.

    구현 이유:
        토큰화는 질의 쪽과 문서 쪽이 반드시 같아야 하므로 함수를 하나만 둔다.
        두 벌로 나뉘면 한쪽만 고쳐졌을 때 겹침이 사라지고, 그것은 "검색이 못 찾았다"로
        보인다.

    트레이드오프:
        대소문자를 접는다(`casefold`). `ISMS` 와 `isms` 를 같게 보는 대신 대소문자로만
        구분되는 약어를 구별하지 못한다. 사내 규정에 그런 쌍은 없다.

    엣지 케이스:
        - 한 글자 토큰: 그 글자를 그대로 토큰으로 쓴다.
        - 숫자와 한글이 붙은 토큰(`제48조의3`): 경계로 쪼개지 않고 통째로 2-gram 을
          만든다. 식별자의 정확 일치가 이 방식의 강점이다.
        - 빈 문자열: 빈 튜플.
    """
    out: list[str] = []
    for token in _TOKEN_SPLIT.split(text.casefold()):
        if not token:
            continue
        if len(token) < NGRAM_SIZE:
            out.append(token)
            continue
        out.extend(token[i : i + NGRAM_SIZE] for i in range(len(token) - NGRAM_SIZE + 1))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Bm25Index:
    """문단 집합 하나에 대한 BM25 색인. 불변이며 문단이 바뀌면 다시 만든다."""

    doc_ids: tuple[str, ...]
    """색인된 문단의 식별자. 순서가 아래 배열들의 인덱스와 일치한다."""
    term_freqs: tuple[Mapping[str, int], ...]
    doc_lengths: tuple[int, ...]
    doc_freqs: Mapping[str, int]
    average_length: float

    def __len__(self) -> int:
        """색인된 문단 수."""
        return len(self.doc_ids)


def build_index(documents: Iterable[tuple[str, str]]) -> Bm25Index:
    """`(식별자, 텍스트)` 목록으로 BM25 색인을 만든다.

    목적:
        검색 대상 문단 집합을 한 번 훑어 용어 빈도·문서 빈도·평균 길이를 확정한다.

    구현 이유:
        문서 빈도(IDF 의 재료)는 **검색 대상 집합에 대해** 계산돼야 한다. 시점
        파라미터로 대상이 달라지면 IDF 도 달라지는 것이 옳다 — 과거 시점에 없던
        문서가 흔한 용어를 흔하지 않게 만들지 않는다.

    트레이드오프:
        시점마다 색인을 다시 만든다. 캐시하지 않는 대신 "어느 시점의 색인인지" 를
        헷갈릴 여지가 없다.

    엣지 케이스:
        - 빈 목록: 길이 0 인 색인. `search` 가 빈 결과를 반환한다.
        - 빈 텍스트를 가진 문단: 토큰이 없으므로 어떤 질의에도 걸리지 않는다.
          코퍼스 파서가 빈 본문을 이미 거부하므로 여기 도달하지 않는다.
    """
    doc_ids: list[str] = []
    term_freqs: list[Mapping[str, int]] = []
    doc_lengths: list[int] = []
    doc_freqs: Counter[str] = Counter()

    for identifier, text in documents:
        tokens = bigrams(text)
        counts = Counter(tokens)
        doc_ids.append(identifier)
        term_freqs.append(counts)
        doc_lengths.append(len(tokens))
        doc_freqs.update(counts.keys())

    total = sum(doc_lengths)
    average = total / len(doc_lengths) if doc_lengths else 0.0
    return Bm25Index(
        doc_ids=tuple(doc_ids),
        term_freqs=tuple(term_freqs),
        doc_lengths=tuple(doc_lengths),
        doc_freqs=dict(doc_freqs),
        average_length=average,
    )


def _idf(index: Bm25Index, term: str) -> float:
    """Lucene 계열 BM25 IDF. `df` 가 0이거나 N 에 가까워도 음수가 되지 않는다."""
    total = len(index)
    freq = index.doc_freqs.get(term, 0)
    return math.log(1.0 + (total - freq + 0.5) / (freq + 0.5))


def search(index: Bm25Index, query: str, *, limit: int) -> tuple[tuple[str, float], ...]:
    """질의에 대한 BM25 상위 문단을 `(식별자, 점수)` 로 반환한다.

    목적:
        어휘 겹침 기준의 순위를 낸다. 하이브리드 결합의 한 축이다.

    구현 이유:
        점수를 정규화하지 않고 그대로 돌려준다. BM25 점수는 질의 길이에 비례해
        커지므로 케이스 간 절대 비교가 무의미하며, 정규화하면 비교 가능한 것처럼
        보이는 값이 생긴다. 결합은 순위 기반(RRF)으로만 한다.

    트레이드오프:
        전수 스캔이다. 역색인을 만들면 빨라지지만, 색인 자체를 매 시점 새로 만드는
        구조에서는 역색인 구축 비용이 스캔 비용을 넘는다.

    엣지 케이스:
        - 어느 문단도 질의 토큰을 갖지 않음: 전부 0점이므로 빈 결과를 반환한다.
          0점 문단을 순위에 올리면 "찾았다"가 아니라 "아무거나 골랐다"가 된다.
        - 동점: 식별자 순으로 안정 정렬한다. 실행마다 순위가 흔들리면 재현이 깨진다.
        - `limit` 이 문단 수보다 큼: 있는 만큼만 반환한다.
    """
    query_terms = set(bigrams(query))
    if not query_terms or not len(index):
        return ()

    scored: list[tuple[str, float]] = []
    for position, identifier in enumerate(index.doc_ids):
        freqs = index.term_freqs[position]
        length = index.doc_lengths[position]
        denominator_length = (
            BM25_K1 * (1.0 - BM25_B + BM25_B * length / index.average_length)
            if index.average_length
            else BM25_K1
        )
        score = 0.0
        for term in query_terms:
            freq = freqs.get(term, 0)
            if freq == 0:
                continue
            score += _idf(index, term) * (freq * (BM25_K1 + 1.0)) / (freq + denominator_length)
        if score > 0.0:
            scored.append((identifier, score))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return tuple(scored[:limit])


def rank_only(results: Sequence[tuple[str, float]]) -> tuple[str, ...]:
    """점수를 버리고 순위만 남긴다. RRF 입력을 만들 때 쓴다."""
    return tuple(identifier for identifier, _ in results)
