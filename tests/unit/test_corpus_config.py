"""코퍼스 설정 로더와 실제 `config/corpus.yaml`의 스키마를 검증한다.

이 테스트가 존재하는 이유: 감시 대상이 설정 파일에 있으므로(ADR-008), 파일이 틀리면
감시 대상이 조용히 빠진다. 그 상태는 "그날 개정 없음"과 구별되지 않는다(ADR-005 R-11).
특히 `law_id`의 앞자리 0은 YAML에서 따옴표를 빠뜨리면 사라지는데, 그렇게 되면 존재하지
않는 법령을 감시하게 되고 아무 오류도 나지 않는다.
"""

from pathlib import Path

import pytest

from regchange.config import CorpusConfigError, load_corpus_config

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG = REPO_ROOT / "config" / "corpus.yaml"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "corpus.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- 실제 설정 파일 -------------------------------------------------------------


def test_real_config_loads() -> None:
    """저장소의 config/corpus.yaml이 스키마를 만족한다."""
    config = load_corpus_config(REAL_CONFIG)
    assert config.version == 1
    assert {c.key for c in config.corpora} == {"infosec", "aml"}


def test_real_config_has_exactly_one_active_corpus() -> None:
    """활성 코퍼스는 infosec 하나여야 한다 (ADR-008). aml은 확장 증명용으로 비활성."""
    config = load_corpus_config(REAL_CONFIG)
    active = config.active_corpora
    assert [c.key for c in active] == ["infosec"]


def test_real_config_law_ids_keep_leading_zeros() -> None:
    """법령ID가 6자리로 보존된다. 정보통신망법 000030의 앞자리 0이 핵심 사례다."""
    config = load_corpus_config(REAL_CONFIG)
    ids = config.active_law_ids
    assert "000030" in ids, "정보통신망법 ID의 앞자리 0이 사라졌다"
    assert all(len(i) == 6 and i.isdigit() for i in ids)


def test_real_config_matches_adr008_corpus() -> None:
    """ADR-008이 확정한 9종 법령이 그대로 들어 있다."""
    config = load_corpus_config(REAL_CONFIG)
    expected = {
        "000030",
        "004797",
        "011357",
        "011468",
        "010199",
        "010366",
        "001540",
        "004105",
        "011359",
    }
    assert set(config.active_law_ids) == expected


def test_real_config_admrule_name_is_the_verified_one() -> None:
    """은행 감독 세칙의 명칭 오류를 막는다.

    `은행업감독규정시행세칙`으로 검색하면 0건이며, 정확한 명칭은
    `은행업감독업무시행세칙`이다 (ADR-006).
    """
    config = load_corpus_config(REAL_CONFIG)
    infosec = next(c for c in config.corpora if c.key == "infosec")
    names = {r.name for r in infosec.admrules}
    assert "은행업감독업무시행세칙" in names
    assert "은행업감독규정시행세칙" not in names


# --- 스키마 검증 ---------------------------------------------------------------


def test_rejects_unquoted_law_id(tmp_path: Path) -> None:
    """따옴표 없는 law_id가 정수로 파싱되면 거부한다.

    PyYAML은 자릿수가 전부 0~7인 값을 8진수로 해석한다. 코퍼스의 정보통신망법
    `000030`이 정확히 그 경우이며, 따옴표를 빼면 **24**가 된다. `009244`처럼 8이나
    9가 섞이면 문자열로 남아 통과하므로, 이 함정은 특정 ID에서만 발현되어 더 위험하다.
    """
    path = write(
        tmp_path,
        "version: 1\ncorpora:\n  x:\n    label: L\n    active: true\n"
        "    laws:\n      - { law_id: 000030, name: N }\n",
    )
    with pytest.raises(CorpusConfigError, match="law_id"):
        load_corpus_config(path)


def test_unquoted_law_id_with_digit_8_or_9_survives_as_string(tmp_path: Path) -> None:
    """8·9가 섞인 ID는 YAML이 문자열로 남긴다 — 함정이 일관되지 않다는 증거."""
    path = write(
        tmp_path,
        "version: 1\ncorpora:\n  x:\n    label: L\n    active: true\n"
        "    laws:\n      - { law_id: 009244, name: N }\n",
    )
    assert load_corpus_config(path).active_law_ids == ("009244",)


def test_rejects_malformed_law_id(tmp_path: Path) -> None:
    """6자리가 아닌 law_id를 거부한다."""
    path = write(
        tmp_path,
        "version: 1\ncorpora:\n  x:\n    label: L\n    active: true\n"
        '    laws:\n      - { law_id: "9244", name: N }\n',
    )
    with pytest.raises(CorpusConfigError, match="6자리"):
        load_corpus_config(path)


def test_rejects_no_active_corpus(tmp_path: Path) -> None:
    """활성 코퍼스가 없으면 거부한다. 감시 대상 0건은 '개정 없음'으로 위장한다."""
    path = write(
        tmp_path,
        "version: 1\ncorpora:\n  x:\n    label: L\n    active: false\n"
        '    laws:\n      - { law_id: "009244", name: N }\n',
    )
    with pytest.raises(CorpusConfigError, match="활성"):
        load_corpus_config(path)


def test_rejects_duplicate_law_id_within_corpus(tmp_path: Path) -> None:
    """한 코퍼스 안의 중복은 오타로 보고 거부한다."""
    path = write(
        tmp_path,
        "version: 1\ncorpora:\n  x:\n    label: L\n    active: true\n"
        '    laws:\n      - { law_id: "009244", name: A }\n'
        '      - { law_id: "009244", name: B }\n',
    )
    with pytest.raises(CorpusConfigError, match="중복"):
        load_corpus_config(path)


def test_rejects_unsupported_version(tmp_path: Path) -> None:
    """스키마 버전이 다르면 거부한다."""
    path = write(tmp_path, "version: 99\ncorpora: {}\n")
    with pytest.raises(CorpusConfigError, match="버전"):
        load_corpus_config(path)


def test_error_message_points_at_the_offending_item(tmp_path: Path) -> None:
    """오류 메시지에 어느 코퍼스의 몇 번째 항목인지가 담긴다."""
    path = write(
        tmp_path,
        "version: 1\ncorpora:\n  infosec:\n    label: L\n    active: true\n"
        '    laws:\n      - { law_id: "000030", name: A }\n'
        '      - { law_id: "bad", name: B }\n',
    )
    with pytest.raises(CorpusConfigError, match=r"corpora\.infosec\.laws\[1\]"):
        load_corpus_config(path)


def test_cross_corpus_duplicate_is_allowed(tmp_path: Path) -> None:
    """코퍼스 간 중복은 허용한다. 코퍼스는 관점이며 배타적 분할이 아니다."""
    path = write(
        tmp_path,
        "version: 1\ncorpora:\n"
        '  a:\n    label: A\n    active: true\n    laws:\n      - { law_id: "009244", name: N }\n'
        '  b:\n    label: B\n    active: true\n    laws:\n      - { law_id: "009244", name: N }\n',
    )
    config = load_corpus_config(path)
    assert config.active_law_ids == ("009244",)
