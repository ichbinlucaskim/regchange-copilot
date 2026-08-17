"""`DocumentStore` 의 로컬 파일시스템 구현체.

목적:
    수집한 원문 스냅샷을 로컬 디렉터리에 보관한다. 개발·테스트와, 배포 형태가
    확정되기 전의 운영에 쓴다.

구현 이유:
    **ADR-010은 AWS 구현체만 만들기로 했으나, 그 전에 필요한 것이 하나 더 있다** —
    "테스트용 인메모리/로컬 구현체를 둔다. 두 번째 구현체 역할을 하면서 단위
    테스트에도 쓰인다"가 ADR-010의 완화책이다. 구현체가 하나뿐인 인터페이스는
    실제로 교체 가능한지 검증되지 않으므로, 이 구현이 그 검증을 겸한다.

    키를 경로로 쓰되 **디렉터리 탈출을 차단한다.** 키는 응답 메타데이터에서
    조립되며 그 안에는 법령명 같은 외부 유래 문자열이 들어갈 수 있다. 외부에서
    온 값이 경로가 되는 순간 신뢰 경계를 넘는다 (ADR-012와 같은 발상).

트레이드오프:
    파일시스템이므로 동시 쓰기 원자성을 보장하지 않는다. 임시 파일에 쓰고
    `rename` 하는 방식으로 **부분 기록만은 막았다** — 읽는 쪽이 반쪽 파일을 보는
    것이 조용한 손상이기 때문이다. 프로세스 간 같은 키 경쟁은 막지 않으며,
    호출부가 키를 유일하게 만드는 책임을 진다 (인터페이스 규약).

    비동기 인터페이스를 파일시스템으로 구현하므로 블로킹 I/O 를 `asyncio.to_thread`
    로 내보낸다. 스레드 전환 비용(수십 µs)이 생기지만, 호출 간격이 1.2초 이상이라
    비교가 무의미하다. **이벤트 루프를 블로킹하는 쪽을 택하지 않은 이유는 그것이
    "지금은 단일 프로세스이므로 괜찮다"는 가정에 의존하기 때문이다** — 그 가정은
    ADR-010의 SQS 워커 구조에서 깨진다.

엣지 케이스:
    - 키에 `..` 나 절대 경로가 들어오면 `DocumentStoreError`. 루트 밖으로
      나가는 경로를 조용히 정규화하지 않는다 — 정규화하면 의도와 다른 위치에
      쓰고도 성공으로 보인다.
    - 같은 키 재저장: **거부한다.** 원칙 6상 과거 스냅샷을 덮어쓰면 안 되고,
      덮어쓰기는 조용히 이력을 지운다. 호출부는 키에 수집 시점을 포함한다.
    - 없는 키 읽기: `DocumentStoreError`. `None` 을 반환하지 않는다 (R-11).
    - 0바이트 저장: 허용한다. 0바이트 응답이 실재하므로 거부하면 그 사례를
      보관할 수 없다.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path, PurePosixPath


class DocumentStoreError(RuntimeError):
    """스냅샷 저장·조회가 실패했을 때 발생한다."""


class LocalDocumentStore:
    """루트 디렉터리 하나 아래에 스냅샷을 보관한다.

    목적:
        `DocumentStore` Protocol 을 파일시스템으로 만족시킨다.

    구현 이유:
        루트를 생성 시점에 고정하고 절대 경로로 정규화한다. 상대 경로를 그대로
        들고 있으면 프로세스의 작업 디렉터리가 바뀔 때 저장 위치가 조용히
        달라진다.

    트레이드오프:
        루트가 하나뿐이라 여러 저장 계층(핫/콜드)을 표현하지 못한다. 필요해지면
        인스턴스를 여러 개 두거나 인터페이스에 계층 개념을 넣는데, **후자는
        인터페이스가 특정 구현에 맞춰 새는 것이므로** 전자를 택한다.

    엣지 케이스:
        - 루트가 없으면 생성한다. 첫 실행에서 실패하지 않게 한다.
        - 루트가 파일이면 `DocumentStoreError`. 디렉터리를 기대한 자리에 파일이
          있는 것은 설정 오류이며 조용히 넘기지 않는다.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        """루트 디렉터리를 고정하고 존재를 보장한다.

        엣지 케이스:
            - 루트 경로에 파일이 있으면 `DocumentStoreError` 를 던진다.
        """
        resolved = root.expanduser().resolve()
        if resolved.exists() and not resolved.is_dir():
            raise DocumentStoreError(f"루트가 디렉터리가 아니다: {resolved}")
        resolved.mkdir(parents=True, exist_ok=True)
        self._root = resolved

    def _resolve(self, key: str) -> Path:
        """키를 루트 안의 경로로 변환하고, 루트를 벗어나면 거부한다.

        목적:
            외부 유래 문자열이 경로가 되는 지점을 한 곳으로 모아 검사한다.

        구현 이유:
            `Path.resolve()` 뒤에 `is_relative_to` 로 확인한다. 문자열 검사로
            `..` 를 찾는 방식은 인코딩 변형과 심링크에 우회된다 — **경로를 실제로
            해석한 뒤 위치를 확인하는 것만이 신뢰할 수 있다.**

        트레이드오프:
            심링크를 따라가므로 루트 안의 심링크가 밖을 가리키면 거부된다.
            정상 사용에서 스냅샷 디렉터리에 심링크를 두지 않으므로 이 엄격함의
            비용이 없다.

        엣지 케이스:
            - 빈 키, 절대 경로, `..` 포함, 루트 밖 해석: 전부
              `DocumentStoreError`.
            - 키에 `/` 가 있으면 하위 디렉터리로 해석한다. 이것은 의도된
              동작이며 계층적 키를 허용한다.
        """
        if not key or not key.strip():
            raise DocumentStoreError("키가 비어 있다")
        pure = PurePosixPath(key)
        if pure.is_absolute() or ".." in pure.parts:
            raise DocumentStoreError(
                f"키가 루트를 벗어난다: {key!r}. 조용히 정규화하지 않는다 — "
                "정규화하면 의도와 다른 위치에 쓰고도 성공으로 보인다"
            )
        candidate = (self._root / pure).resolve()
        if not candidate.is_relative_to(self._root):
            raise DocumentStoreError(f"키가 루트를 벗어난다: {key!r}")
        return candidate

    async def put(self, key: str, body: bytes) -> None:
        """스냅샷을 저장한다. 같은 키가 이미 있으면 거부한다.

        목적:
            수집한 원문을 변형 없이 보관한다.

        구현 이유:
            임시 파일에 쓰고 `replace` 로 옮긴다. 직접 쓰다가 중단되면 반쪽 파일이
            남고, 읽는 쪽은 그것을 정상 스냅샷으로 본다 — **조용한 손상**이다.

            **덮어쓰기를 거부한다.** 원칙 6상 과거 레코드를 덮어쓰지 않으며,
            스냅샷도 같다. 덮어쓰기를 허용하면 재실행이 이력을 조용히 지운다.

        트레이드오프:
            존재 검사와 `replace` 사이에 경쟁 구간이 있어 완전한 원자성은 없다.
            프로세스 간 락을 도입하지 않은 대신 **부분 기록만은 막았고**, 키
            유일성은 호출부 책임으로 규약화했다.

        엣지 케이스:
            - 키 중복: `DocumentStoreError`.
            - 0바이트 본문: 저장한다.
            - 하위 디렉터리가 없으면 생성한다.
        """
        target = self._resolve(key)
        await asyncio.to_thread(self._write_atomically, target, body, key)

    @staticmethod
    def _write_atomically(target: Path, body: bytes, key: str) -> None:
        """임시 파일에 쓰고 옮긴다. 블로킹 I/O 전부를 이 동기 함수에 모은다."""
        if target.exists():
            raise DocumentStoreError(
                f"이미 존재하는 키다: {key!r}. 덮어쓰지 않는다 — 과거 스냅샷을 "
                "덮어쓰면 이력이 조용히 사라진다 (원칙 6). 키에 수집 시점을 포함하라"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            handle.write(body)
            staged = Path(handle.name)
        staged.replace(target)

    async def get(self, key: str) -> bytes:
        """저장한 스냅샷을 바이트로 반환한다.

        목적:
            감사·재현 시 "시스템이 본 원문"을 되읽는다.

        구현 이유:
            바이트를 그대로 반환한다. 디코딩하지 않으므로 해시 대조가 성립하고,
            인코딩 선언을 신뢰하지 않기로 한 결정과 정합한다 (edge-case #15).

        트레이드오프:
            전량을 메모리에 적재한다. 인터페이스 규약과 같은 이유로 스트리밍을
            포기했다.

        엣지 케이스:
            - 없는 키: `DocumentStoreError`. `None` 이나 빈 바이트를 반환하지
              않는다 — 빈 응답과 없는 키를 섞으면 R-11과 같은 혼동이 생긴다.
        """
        target = self._resolve(key)
        return await asyncio.to_thread(self._read, target, key)

    @staticmethod
    def _read(target: Path, key: str) -> bytes:
        """파일을 읽는다. 블로킹 I/O 를 이 동기 함수에 모은다."""
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise DocumentStoreError(
                f"키가 없다: {key!r}. 빈 바이트를 반환하지 않는다 — "
                "'없는 키'와 '0바이트 응답'은 다른 사실이다"
            ) from exc
