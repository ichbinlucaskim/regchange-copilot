"""마이그레이션 파일 로딩 — 이름 규칙과 해시. DB 없이 검사한다.

이 테스트가 존재하는 이유: 마이그레이션이 조용히 적용되지 않는 경로를 막는다.
파일명 규칙에 맞지 않는 파일을 무시하면 그 파일은 영영 적용되지 않고, 아무도
그것을 모른다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from regchange.store.migrate import (
    MigrationError,
    default_migrations_dir,
    load_migration_files,
)


def test_repository_migrations_load_in_order() -> None:
    """저장소의 마이그레이션이 번호 순서로 읽힌다."""
    files = load_migration_files()
    names = [file.filename for file in files]

    assert names == sorted(names)
    assert names[0].startswith("001_")


def test_sha256_matches_file_bytes() -> None:
    """기록되는 해시가 파일 내용의 해시다. 사후 수정 감지의 근거다."""
    directory = default_migrations_dir()
    for file in load_migration_files():
        expected = hashlib.sha256((directory / file.filename).read_bytes()).hexdigest()
        assert file.sha256 == expected


def test_bad_filename_fails_instead_of_being_skipped(tmp_path: Path) -> None:
    """규칙에 맞지 않는 파일은 무시하지 않고 실패시킨다."""
    (tmp_path / "001_ok.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "oops.sql").write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(MigrationError, match="파일명 규칙 위반"):
        load_migration_files(tmp_path)


def test_empty_directory_fails(tmp_path: Path) -> None:
    """파일이 0건이면 '적용할 것이 없음'으로 통과시키지 않는다."""
    with pytest.raises(MigrationError, match="마이그레이션 파일이 없다"):
        load_migration_files(tmp_path)


def test_missing_directory_fails(tmp_path: Path) -> None:
    """경로가 틀리면 즉시 실패한다."""
    with pytest.raises(MigrationError, match="디렉터리가 없다"):
        load_migration_files(tmp_path / "nope")
