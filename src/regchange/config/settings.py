"""실행 환경 설정 — `.env` 로딩과 경로·자격증명 조회를 한 곳에 모은다.

목적:
    `.env` 파일을 프로세스 환경에 얹고, 스냅샷 루트·법제처 OC·기반 URL 처럼
    **환경마다 달라지는 값**을 한 함수로 제공한다.

구현 이유:
    이 모듈이 생긴 직접적인 이유는 **cron 이다.** cron/launchd 는 사용자 셸 환경을
    상속하지 않으므로 `.env` 가 로딩되지 않고, 그러면 `DATABASE_URL` 이 없어
    로컬 기본값으로 떨어지거나 `LAW_GO_KR_OC` 가 없어 KeyError 로 죽는다.
    래퍼 셸 스크립트에서 `export $(cat .env)` 로 처리하는 방법도 있지만, 그러면
    **주석과 따옴표 처리 규칙이 셸에 흩어지고** 수동 실행과 cron 실행의 환경이
    갈린다. 파이썬 안에서 로딩하면 두 경로가 같은 코드를 지난다.

    `python-dotenv` 를 쓰지 않는다. 파싱 규칙이 20줄이고, 의존성을 하나 늘리는
    대신 그 20줄을 읽을 수 있게 두는 쪽을 택했다 (`config/corpus.py` 가 pydantic
    대신 손으로 검증하는 것과 같은 판단).

    **스냅샷 루트를 환경변수로 뺀 이유는 AWS 이관이다.** 로컬에서는
    `data/snapshots` 지만 배포 시에는 S3 버킷/프리픽스가 된다. 코드에 절대 경로가
    박혀 있으면 그 교체가 코드 수정이 되고, 코드 수정은 리뷰와 배포를 요구한다.
    매니페스트의 `directory` 는 이미 루트 기준 상대 경로(`run_id/target/key`)이므로
    루트만 바꾸면 참조가 깨지지 않는다.

트레이드오프:
    `.env` 값을 `os.environ` 에 **덮어쓰지 않고** 채워 넣기만 한다(setdefault).
    이미 설정된 환경변수가 이긴다. 파일이 이기게 하면 운영에서 시크릿 매니저가
    주입한 값을 개발용 `.env` 가 덮는 사고가 가능해진다 — 그 사고는 조용하다.

    반대 방향의 대가도 있다: 셸에 낡은 값이 남아 있으면 `.env` 를 고쳐도 반영되지
    않는다. 그 혼란은 시끄럽다(접속 실패). 조용한 사고보다 시끄러운 혼란을 택했다.

엣지 케이스:
    - `.env` 가 없으면 빈 매핑을 돌려준다. 예외로 만들지 않는 이유는 컨테이너·
      CI 처럼 환경변수를 직접 주는 실행이 정상이기 때문이다. 정작 필요한 값이
      없으면 `require_env` 가 그 자리에서 실패한다.
    - `KEY=value  # 주석` 형태에서 값 뒤의 주석을 잘라낸다. `.env.example` 이
      그 형식으로 쓰여 있고, 잘라내지 않으면 `APP_ENV` 가 `local  # local | ...`
      이 된다.
    - 값에 `#` 이 정상적으로 포함되는 경우(비밀번호)는 이 규칙에 깨진다. 따옴표로
      감싸면 보존된다 — 따옴표 안은 주석 절단 대상이 아니다.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
"""저장소 루트. `config` → `regchange` → `src` → 루트."""

DOTENV_FILENAME = ".env"

SNAPSHOT_ROOT_ENV = "SNAPSHOT_ROOT"
DEFAULT_SNAPSHOT_ROOT = REPO_ROOT / "data" / "snapshots"

OC_ENV = "LAW_GO_KR_OC"
BASE_URL_ENV = "LAW_GO_KR_BASE_URL"


class SettingsError(RuntimeError):
    """필요한 설정이 없다. 조용한 기본값으로 덮지 않는다."""


def parse_dotenv(text: str) -> dict[str, str]:
    """`.env` 텍스트를 키-값으로 파싱한다. 빈 줄과 `#` 주석은 건너뛴다.

    목적:
        파일 읽기와 분리해 파싱 규칙만 테스트할 수 있게 한다.

    구현 이유:
        따옴표를 벗기는 처리를 넣었다. 값에 공백이나 `#` 이 들어가는 경우가
        비밀번호에서 실제로 발생하고, 따옴표 없이는 표현할 방법이 없다.

    트레이드오프:
        `export KEY=value` 형태를 지원하지 않는다. 셸 스크립트로도 쓰이는 `.env`
        라면 필요하지만, 이 저장소의 `.env` 는 파이썬만 읽는다. 지원하지 않는
        형식이 조용히 무시되지 않도록 `export ` 로 시작하는 줄은 그대로 키에
        포함되어 조회가 실패하게 둔다 — 무시하면 값이 없는 이유를 찾기 어렵다.

    엣지 케이스:
        - `=` 가 없는 줄: 건너뛴다. 형식이 아니다.
        - 값이 빈 문자열: 담는다. `.env.example` 의 `LLM_PROVIDER=` 처럼 "비어
          있음"이 의미를 갖는다.
        - 같은 키가 두 번: 뒤의 값이 이긴다. 파일 아래쪽이 나중에 쓴 것이다.
    """
    env: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        else:
            value = value.split("#")[0].strip()
        env[key.strip()] = value
    return env


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """`.env` 를 읽어 파싱한다. 없으면 빈 매핑. `os.environ` 은 건드리지 않는다."""
    target = path or REPO_ROOT / DOTENV_FILENAME
    if not target.exists():
        return {}
    return parse_dotenv(target.read_text(encoding="utf-8"))


def apply_dotenv(path: Path | None = None) -> dict[str, str]:
    """`.env` 를 프로세스 환경에 채워 넣는다. 이미 있는 값은 덮지 않는다.

    목적:
        cron/launchd 실행과 셸 실행이 같은 설정을 보게 한다.

    구현 이유:
        `setdefault` 를 쓴다. 모듈 docstring의 트레이드오프 절 참조 — 주입된
        환경변수가 파일보다 우선한다.

    트레이드오프:
        프로세스 전역 상태를 바꾼다. 순수 함수가 아니므로 테스트에서 격리가
        필요하다(`monkeypatch`). 대신 `owner_dsn()` 같은 기존 조회 함수를
        고치지 않아도 된다 — 그 함수들은 이미 `os.environ` 을 본다.

    엣지 케이스:
        - 두 번 호출: 두 번째는 아무것도 바꾸지 않는다. 멱등이다.
    """
    values = load_dotenv(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def require_env(name: str) -> str:
    """환경변수를 읽되 비어 있으면 실패한다.

    설정 누락을 빈 문자열로 넘기면 요청이 나가고 응답이 실패한다. 그 실패는
    "API 가 이상하다"로 오독되므로, 나가기 전에 여기서 멈춘다.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise SettingsError(
            f"환경변수 {name} 이 비어 있다. `.env` 에 채우거나 실행 환경에 주입한다 — "
            "빈 값으로 진행하면 실패가 API 오류로 위장한다"
        )
    return value


def snapshot_root() -> Path:
    """스냅샷 저장 루트. `SNAPSHOT_ROOT` 가 있으면 그것을, 없으면 저장소 안 기본값.

    목적:
        저장 위치를 코드 밖으로 뺀다 (AWS 이관 준비).

    구현 이유:
        기본값을 두는 이유는 로컬 개발에서 설정 없이 돌아가야 하기 때문이다.
        기본값이 없으면 첫 실행이 설정 오류로 실패하고, 그 마찰이 "일단
        절대경로를 박아 넣는" 우회를 부른다.

    트레이드오프:
        기본값이 있으므로 운영에서 `SNAPSHOT_ROOT` 를 잊으면 저장소 안에 쓴다.
        조용한 실패처럼 보이지만, 컨테이너에서는 그 경로가 휘발성이라 다음 배포에
        사라지는 것으로 드러난다. 그 드러남을 감수하고 로컬 편의를 택했다.

    엣지 케이스:
        - 상대 경로를 주면 그대로 돌려준다. `LocalDocumentStore` 가 절대 경로로
          정규화하며, 그 정규화는 프로세스 작업 디렉터리 기준이다 — cron 은 작업
          디렉터리가 다르므로 **운영에서는 절대 경로를 준다.**
    """
    configured = os.environ.get(SNAPSHOT_ROOT_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_SNAPSHOT_ROOT


def law_api_base_url() -> str:
    """법제처 API 의 기반 URL. endpoint(`lawSearch.do`) 앞까지를 돌려준다.

    `.env` 에 전체 URL 이 들어 있어도 동작하게 잘라낸다 — 설정 파일에 어느 쪽이
    들어 있든 같은 값이 나와야 하고, 그 차이로 요청이 404 가 되는 것은 설정
    오류로 보이지 않는다.
    """
    configured = require_env(BASE_URL_ENV)
    if configured.endswith(".do"):
        return configured.rsplit("/", 1)[0]
    return configured.rstrip("/")


def law_api_oc() -> str:
    """법제처 오픈API 사용자 식별값. 없으면 실패한다."""
    return require_env(OC_ENV)
