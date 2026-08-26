"""사내 규정 Markdown 을 조 단위 문단으로 쪼갠다.

목적:
    `evals/corpus/internal-policies/*.md` 형식의 사내 규정 문서를 front matter
    (메타데이터)와 조 단위 문단으로 분해하고, 각 문단의 `text_raw`/`text_norm` 을
    함께 만든다.

구현 이유:
    **이 파서는 `tests/unit/test_policy_corpus.py` 에 있던 것을 승격한 것이다.**
    테스트 안에만 있으면 운영 코드가 쓸 수 없고, 그러면 채점이 보는 조 경계와 검색이
    보는 조 경계가 서로 다른 코드로 정해진다. 두 경계가 어긋나면 지표는 "검색이 못
    찾았다"로 나타나며 원인은 드러나지 않는다. 이제 테스트도 이 함수를 import 한다.

    **청킹 단위를 조로 고정한다.** 근거는 두 가지다.
      (a) 골든셋 `article_spec` 29건이 전부 조 단위다 — `제18조 제3항` 형태가 없다.
          인용 단위가 곧 검증 단위이므로(기획서 8.1 2단), 항으로 쪼개면 채점 시
          조 단위로 되말아야 하고 그 되말기 규칙이 정밀도를 결정하게 된다.
          같은 조의 두 항이 top-k 에 들어왔을 때 한 건인지 두 건인지가 지표를 만든다.
      (b) 현재 코퍼스의 조 길이가 중앙값 148자, 최대 486자다. 잡음이 섞일 만큼 긴
          조가 없어 쪼갤 이유 자체가 발생하지 않는다.

    **길이 임계값을 두지 않는다.** "500자를 넘으면 항으로 쪼갠다" 같은 규칙은 지금
    코퍼스에서 발동 건수가 0이고, 발동하지 않는 코드는 작동하는지 알 수 없는 코드다.
    근거 없는 상수를 하나 만드는 대신 규칙을 만들지 않는다.

    **정규화에 `strip_prefix=False` 를 쓴다.** 조 본문은 `① … ② …` 여러 항으로
    이루어지는데, 접두어 제거 정규식은 문자열 맨 앞 하나만 지운다. 첫 항의 `①` 만
    사라지고 나머지는 남으면 항끼리 비대칭이 되어 bigram 색인에 잡음이 된다.

트레이드오프:
    **실제 은행 규정은 이보다 길다.** 조 단위가 지금 맞는 이유는 코퍼스가 합성이고
    짧기 때문이며, 실제 규정을 넣으면 한 조가 여러 화면을 넘어갈 수 있다. 그때는
    조 단위 청크에 잡음이 섞인다.
    **재검토 신호**: 검색 정밀도가 낮은데 실패 원인이 "틀린 조가 올라왔다"가 아니라
    **"맞는 조인데 그 안에서 관련 없는 부분이 점수를 만들었다"** 일 때. 그 시점에
    이 함수와 채점 단위를 함께 고친다 — 한쪽만 고치면 지표가 조용히 어긋난다.
    관련 한계는 `docs/09-corpus-design.md` 와 `docs/10-retrieval-evaluation-protocol.md` §3.

    가지번호(`제5조의2`)를 다루지 않는다. 사내 문서에는 쓰지 않기로 했고, 지원하면
    정규식이 느슨해져 오탐이 는다. 법령 쪽 조문 경로(`ArticleRef`)는 가지번호를
    다루므로 두 체계가 다르다 — 사내 문서에 가지번호가 들어오는 날 함께 고친다.

엣지 케이스:
    - front matter 없음: `CorpusError`. 메타데이터 없는 문서는 코퍼스가 아니다.
    - 조가 하나도 없음: `CorpusError`. 파싱 실패를 빈 결과로 숨기지 않는다.
    - 조 번호 중복: `CorpusError`. 뒤의 것이 앞의 것을 덮어써 조용히 사라지는 것을 막는다.
    - 제1조 앞의 문서 제목·장 제목: 어느 조에도 속하지 않으므로 버린다.
    - 본문이 빈 조: `CorpusError`. 검색 대상이 없는 문단은 참조만 성립하고 채점되지
      않는다 — 지표에 드러나지 않는 결손이므로 적재 전에 막는다.
    - 필수 메타데이터 누락: `CorpusError`. 특히 `effective_date` 가 없으면 시점 검색이
      그 문서를 조용히 빠뜨리거나 항상 포함시킨다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from regchange.parse.normalize import normalize
from regchange.retrieval.models import PolicyArticle, PolicyDocument

ARTICLE_HEADING = re.compile(r"^###\s+제(\d+)조\(([^)]+)\)\s*$")
"""조 경계. `### 제7조(정보보호 최고책임자의 업무)` 형식만 인정한다.

느슨하게 받으면 문서마다 다른 표기가 섞이고, 검색이 문단 단위로 인용할 때 조 경계가
흐려진다. 표기를 하나로 강제하는 비용은 문서 작성 시 한 번이고, 흐려진 경계의 비용은
채점할 때마다 든다."""

ARTICLE_SPEC = re.compile(r"^제(\d+)조\s*\(([^)]+)\)$")
"""골든셋의 `article_spec` — `제18조 (접속기록의 보관)`. 괄호 앞 공백은 허용한다."""

FRONT_MATTER_FENCE = "---\n"

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
"""`docs/09-corpus-design.md` §6 이 요구하는 문서 메타데이터."""


class CorpusError(ValueError):
    """사내 규정 문서를 파싱할 수 없다. 조용한 부분 결과를 만들지 않는다."""


def parse_article_spec(spec: str) -> tuple[int, str]:
    """골든셋 `article_spec` 을 `(조 번호, 제목)` 으로 쪼갠다.

    목적:
        채점기와 파서가 같은 규칙으로 조를 식별하게 한다.

    구현 이유:
        골든셋과 문서는 서로 다른 파일에 서로 다른 표기로 같은 것을 가리킨다.
        변환 규칙이 두 벌 있으면 한쪽만 고쳐졌을 때 "그 조항을 찾지 못했다"는
        낮은 점수로 나타나고, 지표가 나쁜 것과 참조가 깨진 것을 구별할 수 없다.

    트레이드오프:
        형식을 하나만 받으므로 골든셋 작성 시 표기 자유도가 없다. 자유도를 주면
        표기 흔들림이 조용한 채점 실패가 된다.

    엣지 케이스:
        - 형식 불일치: `CorpusError`. 빈 값이나 `None` 을 돌려주지 않는다.
    """
    match = ARTICLE_SPEC.match(spec.strip())
    if match is None:
        msg = f"article_spec 형식이 아니다: {spec!r}"
        raise CorpusError(msg)
    return int(match.group(1)), match.group(2).strip()


def _split_front_matter(path: Path, raw: str) -> tuple[dict[str, Any], str]:
    """Front matter 와 본문을 나눈다. 형식이 어긋나면 예외를 던진다."""
    if not raw.startswith(FRONT_MATTER_FENCE):
        msg = f"{path.name}: YAML front matter 가 없다"
        raise CorpusError(msg)

    _, front_matter, body = raw.split(FRONT_MATTER_FENCE, 2)
    loaded: Any = yaml.safe_load(front_matter)
    if not isinstance(loaded, dict):
        msg = f"{path.name}: front matter 가 매핑이 아니다"
        raise CorpusError(msg)

    missing = [field for field in METADATA_REQUIRED if field not in loaded]
    if missing:
        msg = f"{path.name}: 메타데이터 누락 {missing}"
        raise CorpusError(msg)
    return loaded, body


def _effective_date(path: Path, value: Any) -> dt.date:
    """`effective_date` 를 날짜로 확정한다. 문자열도 받되 형식을 강제한다."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            msg = f"{path.name}: effective_date 를 날짜로 읽을 수 없다: {value!r}"
            raise CorpusError(msg) from exc
    msg = f"{path.name}: effective_date 형식이 잘못됐다: {value!r}"
    raise CorpusError(msg)


def _articles(path: Path, body: str) -> tuple[PolicyArticle, ...]:
    """본문을 조 단위로 쪼개고 각 조의 정규화본을 만든다."""
    collected: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    number: int | None = None
    title = ""
    buffer: list[str] = []

    def flush() -> None:
        if number is None:
            return
        if number in seen:
            msg = f"{path.name}: 제{number}조가 두 번 나온다"
            raise CorpusError(msg)
        seen.add(number)
        collected.append((number, title, "\n".join(buffer).strip()))

    for line in body.splitlines():
        heading = ARTICLE_HEADING.match(line)
        if heading:
            flush()
            number = int(heading.group(1))
            title = heading.group(2).strip()
            buffer = []
        elif number is not None:
            buffer.append(line)
    flush()

    if not collected:
        msg = f"{path.name}: 조를 하나도 찾지 못했다"
        raise CorpusError(msg)

    articles: list[PolicyArticle] = []
    for seq, (article_no, article_title, text_raw) in enumerate(collected, start=1):
        if not text_raw:
            msg = f"{path.name}: 제{article_no}조의 본문이 비어 있다"
            raise CorpusError(msg)
        # 제목을 본문 앞에 붙여 정규화한다. 조 제목은 그 조가 무엇을 다루는지를 가장
        # 압축적으로 말하며, 검색에서 빼면 "제35조(침해사고 예방점검 및 보고체계)"의
        # 핵심 어휘가 색인에서 사라진다.
        normalized = normalize(f"{article_title}\n{text_raw}", strip_prefix=False)
        articles.append(
            PolicyArticle(
                article_no=article_no,
                article_title=article_title,
                text_raw=text_raw,
                text_norm=normalized.norm,
                text_norm_sha256=normalized.sha256,
                norm_rule_version=normalized.rule_version,
                seq_in_doc=seq,
            )
        )
    return tuple(articles)


def parse_policy_document(path: Path) -> PolicyDocument:
    """사내 규정 문서 하나를 메타데이터와 조 단위 문단으로 분해한다.

    목적:
        골든셋이 `doc_id` + 조 번호로 가리키는 대상을 실제 파일에서 만들어 낸다.
        결과는 그대로 `policy_document` / `policy_paragraph` 적재 입력이 된다.

    구현 이유:
        원본 파일의 sha256 을 함께 담는다(ADR-007). 같은 `doc_id` v5.1 이라도 파일이
        바뀌었으면 다른 코퍼스이며, 그 사실이 검색 결과 차이의 원인일 수 있다.
        해시가 없으면 "어제와 다른 점수가 나왔다"의 원인을 모델·질의·문서 중 어디로
        돌릴지 알 수 없다.

    트레이드오프:
        파일 전체를 메모리에 읽는다. 사내 규정 문서 크기(최대 20KB)에서는 무의미하고,
        스트리밍 파싱은 조 경계 판정을 복잡하게 만든다.

    엣지 케이스:
        모듈 docstring 참조. 모든 실패는 `CorpusError` 이며 부분 결과를 만들지 않는다.
    """
    raw = path.read_text(encoding="utf-8")
    metadata, body = _split_front_matter(path, raw)
    articles = _articles(path, body)

    revision_history = tuple(
        {str(key): str(value) for key, value in entry.items()}
        for entry in metadata["revision_history"]
    )
    return PolicyDocument(
        doc_id=str(metadata["doc_id"]),
        version=str(metadata["version"]),
        title=str(metadata["title"]),
        owner_dept=str(metadata["owner_dept"]),
        classification=str(metadata["classification"]),
        effective_date=_effective_date(path, metadata["effective_date"]),
        parent_laws=tuple(str(law) for law in metadata["parent_laws"]),
        revision_history=revision_history,
        source_path=path.name,
        source_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        articles=articles,
    )


def load_corpus(directory: Path, *, pattern: str = "ISP-*.md") -> tuple[PolicyDocument, ...]:
    """디렉터리의 사내 규정 문서를 전부 파싱한다.

    목적:
        코퍼스 전체를 한 번에 읽어 적재·채점의 입력으로 만든다.

    구현 이유:
        파일명 정렬 순서를 그대로 쓴다. 적재 순서가 결정론적이어야 같은 코퍼스를
        두 번 적재했을 때 `seq_in_doc` 이 흔들리지 않는다.

    트레이드오프:
        기본 패턴이 `ISP-*.md` 라 명명 규칙에 결합된다. 대신 `README.md` 같은
        비-문서 파일을 조용히 파싱하려다 실패하는 일이 없다.

    엣지 케이스:
        - 일치하는 파일이 없음: `CorpusError`. 경로 오류가 "코퍼스가 비었음"으로
          보이면 그 위에서 돌린 측정이 전부 0점으로 나오고 원인은 드러나지 않는다.
        - `doc_id` 중복: `CorpusError`. 두 파일이 같은 문서를 주장하면 검색 결과의
          `doc_id` 로 원본을 되짚을 수 없다.
    """
    paths = sorted(directory.glob(pattern))
    if not paths:
        msg = f"{directory}: {pattern} 에 해당하는 문서가 없다"
        raise CorpusError(msg)

    documents = tuple(parse_policy_document(path) for path in paths)
    seen: set[str] = set()
    for document in documents:
        if document.doc_id in seen:
            msg = f"doc_id 가 중복된다: {document.doc_id}"
            raise CorpusError(msg)
        seen.add(document.doc_id)
    return documents
