"""로컬 스냅샷 저장소가 덮어쓰기와 경로 탈출을 거부하는지 고정한다.

이 파일이 존재하는 이유: 스냅샷을 덮어쓰면 원칙 6(과거를 지우지 않는다)이 무너지고,
키가 루트를 벗어나면 외부 유래 문자열이 경로가 되는 신뢰 경계 위반이 된다.
둘 다 조용히 성공한 것처럼 보이는 실패다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regchange.adapters.storage import DocumentStore
from regchange.adapters.storage.local import DocumentStoreError, LocalDocumentStore


def store(tmp_path: Path) -> LocalDocumentStore:
    return LocalDocumentStore(tmp_path / "snapshots")


def test_local_store_satisfies_the_protocol(tmp_path: Path) -> None:
    """구현체가 인터페이스를 만족하는지 — 두 번째 구현체 검증 역할 (ADR-010)."""
    instance: DocumentStore = store(tmp_path)
    assert instance is not None


async def test_round_trip_preserves_bytes_exactly(tmp_path: Path) -> None:
    """바이트를 그대로 돌려준다 — 해시 대조가 성립해야 한다."""
    subject = store(tmp_path)
    body = "제1조(목적) 이 법은 …".encode()
    await subject.put("lsJoHstInf/20250401/page-1.xml", body)
    assert await subject.get("lsJoHstInf/20250401/page-1.xml") == body


async def test_overwrite_is_refused(tmp_path: Path) -> None:
    """같은 키 재저장을 거부한다 — 덮어쓰기는 이력을 조용히 지운다 (원칙 6)."""
    subject = store(tmp_path)
    await subject.put("a.xml", b"first")
    with pytest.raises(DocumentStoreError, match="덮어쓰지 않는다"):
        await subject.put("a.xml", b"second")
    assert await subject.get("a.xml") == b"first"


async def test_missing_key_raises_instead_of_returning_empty(tmp_path: Path) -> None:
    """없는 키와 0바이트 응답은 다른 사실이다 (R-11과 같은 성질)."""
    with pytest.raises(DocumentStoreError, match="키가 없다"):
        await store(tmp_path).get("nope.xml")


async def test_zero_byte_body_is_stored(tmp_path: Path) -> None:
    """0바이트 응답이 실재하므로 거부하면 그 사례를 보관할 수 없다."""
    subject = store(tmp_path)
    await subject.put("empty.xml", b"")
    assert await subject.get("empty.xml") == b""


ESCAPING_KEYS = [
    pytest.param("../outside.xml", id="상위디렉터리"),
    pytest.param("a/../../outside.xml", id="중간에_상위"),
    pytest.param("/etc/passwd", id="절대경로"),
    pytest.param("", id="빈키"),
    pytest.param("   ", id="공백뿐"),
]


@pytest.mark.parametrize("key", ESCAPING_KEYS)
async def test_escaping_keys_are_refused(tmp_path: Path, key: str) -> None:
    """조용히 정규화하지 않는다 — 정규화하면 의도와 다른 위치에 쓰고도 성공으로 보인다."""
    with pytest.raises(DocumentStoreError):
        await store(tmp_path).put(key, b"x")


async def test_nested_key_creates_subdirectories(tmp_path: Path) -> None:
    subject = store(tmp_path)
    await subject.put("a/b/c/d.xml", b"deep")
    assert (tmp_path / "snapshots" / "a" / "b" / "c" / "d.xml").read_bytes() == b"deep"


def test_root_that_is_a_file_is_refused(tmp_path: Path) -> None:
    """디렉터리를 기대한 자리에 파일이 있는 것은 설정 오류다."""
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")
    with pytest.raises(DocumentStoreError, match="디렉터리가 아니다"):
        LocalDocumentStore(occupied)


async def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    """임시 파일에 쓰고 옮기므로 반쪽 파일이 남지 않는다."""
    subject = store(tmp_path)
    await subject.put("a.xml", b"x")
    files = sorted(p.name for p in (tmp_path / "snapshots").iterdir())
    assert files == ["a.xml"]
