"""법제처 Open API 탐색용 호출 헬퍼 (0.7단계 전용, 재사용 대상이 아니다).

목적:
    구조 파악에 필요한 최소한의 요청을 보내고, 응답을 OC 값이 제거된 상태로
    tests/fixtures/law_api/ 에 저장한다.

구현 이유:
    src/regchange/ 아래에 두지 않는다. 이 코드는 사실 확인을 위한 도구이며,
    여기서 검증된 사실이 이후 파서 설계의 입력이 된다. 지금 재사용 가능한
    클라이언트로 만들면 아직 확정되지 않은 응답 구조를 전제로 추상화하게 된다.

트레이드오프:
    이후 실제 클라이언트를 만들 때 이 파일의 일부가 중복 작성된다. 중복을
    감수한 대신, 탐색 결과가 설계를 강제하지 않도록 했다.

엣지 케이스:
    - OC 값은 저장 직전에 '***' 로 치환한다. 응답 본문에 OC 가 echo 되는
      경우가 있으므로 URL 뿐 아니라 본문 전체를 치환 대상으로 한다.
    - 호출 간 1초 이상 지연을 강제한다. 공공 API 에 부담을 주지 않는다.
    - 비-2xx 응답도 저장한다. 에러 응답의 형태 자체가 조사 대상이다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "law_api"

MIN_DELAY_SECONDS = 1.2
"""호출 간 최소 간격. 요구사항은 1초이며 여유를 둔다."""

REQUEST_TIMEOUT_SECONDS = 30.0

_last_call_at = 0.0


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.split("#")[0].strip()
    return env


ENV = _load_env()
OC = ENV["LAW_GO_KR_OC"]

# .env 의 LAW_GO_KR_BASE_URL 은 lawService.do 까지 포함한 전체 URL 이므로
# 디렉터리 부분만 취해 lawSearch.do / lawService.do 를 조립한다.
_configured = ENV.get("LAW_GO_KR_BASE_URL", "").strip()
BASE = _configured.rsplit("/", 1)[0] if _configured.endswith(".do") else _configured.rstrip("/")


def scrub(text: str) -> str:
    """OC 값을 마스킹한다. 저장·출력 전 반드시 통과시킨다."""
    return text.replace(OC, "***")


def call(
    endpoint: str,
    params: dict[str, Any],
    *,
    save_as: str | None = None,
    base: str | None = None,
) -> httpx.Response:
    """API 를 호출하고 필요하면 픽스처로 저장한다."""
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_DELAY_SECONDS:
        time.sleep(MIN_DELAY_SECONDS - elapsed)

    url = f"{base or BASE}/{endpoint}"
    full = {"OC": OC, **params}
    started = time.monotonic()
    response = httpx.get(url, params=full, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    _last_call_at = time.monotonic()

    safe_query = scrub(urlencode(full))
    print(
        f"[{response.status_code}] {url}?{safe_query}\n"
        f"  {len(response.content)}B  {response.headers.get('content-type', '?')}  "
        f"{_last_call_at - started:.2f}s",
        file=sys.stderr,
    )

    if save_as:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        target = FIXTURE_DIR / save_as
        body = scrub(response.text)
        if OC in body:  # 방어: 치환이 실패하면 저장하지 않는다
            raise RuntimeError(f"OC 마스킹 실패: {save_as}")
        target.write_text(body, encoding="utf-8")
        print(f"  saved -> {target.relative_to(REPO_ROOT)}", file=sys.stderr)

    return response


def show(text: str, limit: int | None = None) -> None:
    """응답을 마스킹해서 출력한다.

    응답 본문의 `법령상세링크` 필드에 요청 OC 가 그대로 echo 되므로,
    raw text 를 직접 출력하면 자격증명이 터미널·로그에 남는다. 탐색 중
    본문을 볼 때는 반드시 이 함수를 쓴다.
    """
    safe = scrub(text)
    print(safe if limit is None else safe[:limit])


def raw_url(endpoint: str, params: dict[str, Any], base: str | None = None) -> str:
    """디버깅용 URL 문자열(마스킹됨)."""
    return scrub(f"{base or BASE}/{endpoint}?{urlencode({'OC': OC, **params})}")
