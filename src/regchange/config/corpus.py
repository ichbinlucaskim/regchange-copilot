"""코퍼스 설정 파일의 스키마와 로더.

목적:
    `config/corpus.yaml`을 읽어 `CorpusConfig`로 반환한다. 형식 위반은 로딩 시점에
    `CorpusConfigError`로 실패시킨다.

구현 이유:
    pydantic 모델 대신 dataclass + 명시적 검증을 썼다. 이 파일의 스키마는 얕고
    (코퍼스 → 법령/행정규칙 목록) 검증 규칙이 도메인 특유하다 — `law_id`의 앞자리
    0 보존, 활성 코퍼스 0건 거부 같은 것은 어차피 손으로 써야 한다. 얕은 스키마에
    검증 프레임워크를 얹으면 규칙이 두 곳(모델 제약과 커스텀 validator)에 흩어진다.

트레이드오프:
    pydantic이 주는 자동 오류 메시지와 JSON 스키마 생성을 포기했다. 대신 오류
    메시지를 직접 쓰되, **어느 코퍼스의 몇 번째 항목이 왜 틀렸는지**를 항상 담는다.
    설정 오류는 운영자가 고쳐야 하므로 위치 정보가 없는 메시지는 쓸모가 없다.

엣지 케이스:
    - 파일이 없거나 YAML이 깨진 경우: 예외를 전파한다. 기본값으로 빈 코퍼스를
      돌려주지 않는다 — 감시 대상 0건은 "개정 없음"으로 위장한다 (ADR-005 R-11).
    - `active: true`인 코퍼스가 없으면 거부한다.
    - `law_id`가 6자리 숫자 문자열이 아니면 거부한다. PyYAML은 따옴표 없는 값을
      **일관되지 않게** 파싱한다 — 자릿수가 전부 0~7이면 8진수로 해석해
      `000030`이 정수 `24`가 되지만, 8이나 9가 섞인 `009244`는 문자열로 남는다.
      대부분의 ID에서 정상 동작하다가 특정 ID에서만 조용히 다른 값이 되므로
      타입 검사를 반드시 한다. 코퍼스의 정보통신망법 `000030`이 그 경우다.
    - 한 코퍼스 안의 `law_id` 중복은 거부한다(오타). 코퍼스 간 중복은 허용한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LAW_ID_PATTERN = re.compile(r"^\d{6}$")
"""법령ID는 6자리 숫자다. 실측 관측 범위: 000030 ~ 014044 (0.8단계 CSV 3,081행)."""

SUPPORTED_VERSION = 1
"""설정 파일 스키마 버전. 구조가 바뀌면 올리고 로더가 거부하게 한다."""

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "corpus.yaml"


class CorpusConfigError(ValueError):
    """코퍼스 설정이 스키마를 만족하지 않을 때 발생한다."""


@dataclass(frozen=True)
class LawRef:
    """감시 대상 법령 하나. `law_id`가 식별자이며 `name`은 사람이 읽기 위한 것이다."""

    law_id: str
    name: str


@dataclass(frozen=True)
class AdmRule:
    """감시 대상 행정규칙 하나.

    행정규칙은 조문 수준 식별자가 없고 목록 조회도 이름 기반이므로(ADR-006),
    `law_id`에 해당하는 안정적 키가 없다. `org_code`는 검색 노이즈를 거르는 데
    쓰이며 없을 수 있다.
    """

    name: str
    org: str
    org_code: str | None = None


@dataclass(frozen=True)
class Corpus:
    """코퍼스 하나. 활성/비활성은 설정으로 토글한다 (ADR-008)."""

    key: str
    label: str
    active: bool
    laws: tuple[LawRef, ...] = field(default_factory=tuple)
    admrules: tuple[AdmRule, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CorpusConfig:
    """설정 파일 전체."""

    version: int
    corpora: tuple[Corpus, ...]

    @property
    def active_corpora(self) -> tuple[Corpus, ...]:
        """활성 코퍼스만 반환한다."""
        return tuple(c for c in self.corpora if c.active)

    @property
    def active_law_ids(self) -> tuple[str, ...]:
        """활성 코퍼스의 법령ID 전체. 코퍼스 간 중복은 제거하되 순서는 보존한다."""
        seen: dict[str, None] = {}
        for corpus in self.active_corpora:
            for law in corpus.laws:
                seen.setdefault(law.law_id, None)
        return tuple(seen)


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorpusConfigError(f"{where}: 매핑이어야 하는데 {type(value).__name__}이다")
    return value


def _parse_law(raw: Any, where: str) -> LawRef:
    item = _require_mapping(raw, where)
    law_id = item.get("law_id")
    if not isinstance(law_id, str):
        raise CorpusConfigError(
            f"{where}: law_id는 따옴표로 감싼 문자열이어야 한다 "
            f"(현재 {law_id!r}). PyYAML이 0~7로만 된 값을 8진수로 해석한다"
        )
    if not LAW_ID_PATTERN.match(law_id):
        raise CorpusConfigError(f"{where}: law_id가 6자리 숫자가 아니다 ({law_id!r})")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CorpusConfigError(f"{where}: name이 비어 있다")
    return LawRef(law_id=law_id, name=name.strip())


def _parse_admrule(raw: Any, where: str) -> AdmRule:
    item = _require_mapping(raw, where)
    name = item.get("name")
    org = item.get("org")
    if not isinstance(name, str) or not name.strip():
        raise CorpusConfigError(f"{where}: name이 비어 있다")
    if not isinstance(org, str) or not org.strip():
        raise CorpusConfigError(f"{where}: org가 비어 있다")
    org_code = item.get("org_code")
    if org_code is not None and not isinstance(org_code, str):
        raise CorpusConfigError(f"{where}: org_code는 문자열이어야 한다 ({org_code!r})")
    return AdmRule(name=name.strip(), org=org.strip(), org_code=org_code)


def _parse_corpus(key: str, raw: Any) -> Corpus:
    body = _require_mapping(raw, f"corpora.{key}")
    label = body.get("label")
    if not isinstance(label, str) or not label.strip():
        raise CorpusConfigError(f"corpora.{key}: label이 비어 있다")
    active = body.get("active")
    if not isinstance(active, bool):
        raise CorpusConfigError(f"corpora.{key}: active는 true/false여야 한다 ({active!r})")

    laws = tuple(
        _parse_law(item, f"corpora.{key}.laws[{i}]")
        for i, item in enumerate(body.get("laws") or [])
    )
    duplicates = {law.law_id for law in laws if [x.law_id for x in laws].count(law.law_id) > 1}
    if duplicates:
        raise CorpusConfigError(f"corpora.{key}: law_id가 중복됐다 {sorted(duplicates)}")

    admrules = tuple(
        _parse_admrule(item, f"corpora.{key}.admrules[{i}]")
        for i, item in enumerate(body.get("admrules") or [])
    )
    return Corpus(key=key, label=label.strip(), active=active, laws=laws, admrules=admrules)


def load_corpus_config(path: Path | None = None) -> CorpusConfig:
    """코퍼스 설정을 읽어 검증된 값으로 반환한다.

    목적:
        수집기와 평가 러너가 감시 대상을 알 수 있게 한다.

    구현 이유:
        검증을 로딩 시점에 전부 수행하고 부분 로딩을 허용하지 않는다. 감시 대상이
        일부만 실린 상태로 수집이 돌면 빠진 법령의 개정이 "없었던 일"이 된다.

    트레이드오프:
        설정에 오타 하나만 있어도 기동이 실패한다. 관대한 로딩을 포기한 대신
        조용한 누락을 없앴다. 규제 도메인에서 조용한 실패는 오답보다 나쁘다
        (CLAUDE.md §4).

    엣지 케이스:
        - 파일 부재·YAML 파싱 실패: 예외를 전파한다.
        - `version`이 지원 범위 밖: 거부한다.
        - 활성 코퍼스 0건: 거부한다.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document = _require_mapping(raw, str(config_path))

    version = document.get("version")
    if version != SUPPORTED_VERSION:
        raise CorpusConfigError(
            f"지원하지 않는 설정 버전이다 (현재 {version!r}, 지원 {SUPPORTED_VERSION})"
        )

    corpora_raw = _require_mapping(document.get("corpora"), "corpora")
    if not corpora_raw:
        raise CorpusConfigError("corpora가 비어 있다")

    corpora = tuple(_parse_corpus(key, body) for key, body in corpora_raw.items())
    config = CorpusConfig(version=version, corpora=corpora)

    if not config.active_corpora:
        raise CorpusConfigError(
            "활성(active: true) 코퍼스가 하나도 없다. "
            "감시 대상 0건은 '개정 없음'과 구별되지 않는다 (ADR-005)"
        )
    return config
