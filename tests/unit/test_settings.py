"""`.env` 파싱과 경로 설정 — cron 첫날 실패의 가장 흔한 원인.

이 테스트가 존재하는 이유: cron/launchd 는 사용자 셸 환경을 상속하지 않는다. `.env`
로딩이 조용히 틀리면 `DATABASE_URL` 이 로컬 기본값으로 떨어지거나 OC 가 빈 문자열로
나가고, 후자는 **API 오류로 위장한다**. 그리고 `SNAPSHOT_ROOT` 는 AWS 이관에서
S3 프리픽스로 바뀔 값이므로 코드에 박히지 않았는지 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regchange.config.settings import (
    DEFAULT_SNAPSHOT_ROOT,
    SNAPSHOT_ROOT_ENV,
    SettingsError,
    apply_dotenv,
    law_api_base_url,
    parse_dotenv,
    require_env,
    snapshot_root,
)


def test_trailing_comment_is_stripped() -> None:
    """`.env.example` 이 `KEY=value  # 설명` 형식이다. 자르지 않으면 값이 오염된다."""
    assert parse_dotenv("APP_ENV=local            # local | staging") == {"APP_ENV": "local"}


def test_quoted_value_keeps_hash_and_spaces() -> None:
    """비밀번호에 `#` 이 들어갈 수 있다. 따옴표 안은 주석 절단 대상이 아니다."""
    assert parse_dotenv('PASSWORD="a b#c"') == {"PASSWORD": "a b#c"}


def test_comment_and_blank_lines_are_ignored() -> None:
    assert parse_dotenv("# 주석\n\nA=1\n") == {"A": "1"}


def test_empty_value_is_kept() -> None:
    """`LLM_PROVIDER=` 처럼 '비어 있음'이 의미를 갖는다."""
    assert parse_dotenv("LLM_PROVIDER=") == {"LLM_PROVIDER": ""}


def test_existing_environment_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """주입된 환경변수가 파일보다 우선한다.

    반대로 두면 시크릿 매니저가 준 값을 개발용 `.env` 가 덮는 사고가 가능해지고,
    그 사고는 조용하다.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("SOME_KEY", "from-environment")

    apply_dotenv(env_file)

    assert require_env("SOME_KEY") == "from-environment"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """컨테이너·CI 는 환경변수를 직접 준다. 파일 부재는 정상이다."""
    assert apply_dotenv(tmp_path / "없는파일") == {}


def test_missing_setting_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """빈 값으로 요청을 보내면 실패가 API 오류로 위장한다."""
    monkeypatch.setenv("LAW_GO_KR_OC", "")
    with pytest.raises(SettingsError, match="LAW_GO_KR_OC"):
        require_env("LAW_GO_KR_OC")


def test_snapshot_root_is_overridable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS 이관에서 이 값이 S3 프리픽스가 된다. 코드에 박혀 있으면 안 된다."""
    monkeypatch.setenv(SNAPSHOT_ROOT_ENV, str(tmp_path / "snap"))
    assert snapshot_root() == tmp_path / "snap"


def test_snapshot_root_falls_back_to_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정이 없으면 저장소 안 기본값. 첫 실행이 설정 오류로 막히지 않게 한다."""
    monkeypatch.delenv(SNAPSHOT_ROOT_ENV, raising=False)
    assert snapshot_root() == DEFAULT_SNAPSHOT_ROOT


def test_base_url_accepts_both_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` 에 endpoint 까지 들어 있어도 같은 값이 나와야 한다."""
    monkeypatch.setenv("LAW_GO_KR_BASE_URL", "https://www.law.go.kr/DRF/lawSearch.do")
    assert law_api_base_url() == "https://www.law.go.kr/DRF"

    monkeypatch.setenv("LAW_GO_KR_BASE_URL", "https://www.law.go.kr/DRF/")
    assert law_api_base_url() == "https://www.law.go.kr/DRF"
