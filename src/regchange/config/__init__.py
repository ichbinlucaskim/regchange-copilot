"""설정 경계 — 감시 대상 코퍼스와 실행 환경.

목적:
    `config/corpus.yaml`에 선언된 감시 대상(법령ID 목록, 행정규칙 목록)을 읽어
    타입이 붙은 값으로 돌려주고, 형식이 어긋나면 로딩 시점에 실패시킨다.
    `.env`와 환경변수(스냅샷 루트, 법제처 OC)도 이 경계에서 읽는다.

구현 이유:
    코퍼스를 코드가 아니라 설정에 둔다. 도메인 선택은 실측 데이터에 따라 바뀔 수
    있는 결정이며(ADR-008), 바뀔 때 코드 변경과 배포가 필요하면 "되돌리는 비용이
    낮다"는 ADR-008의 전제가 성립하지 않는다. 설정 파일이면 한 줄 수정이다.

    **"무엇을 감시하는가"(corpus)와 "어디서 도는가"(settings)를 한 패키지에 둔
    이유**는 둘 다 *배포 대상이 아닌 값*이기 때문이다. 코드에 박히면 환경마다
    다른 빌드가 필요해지고, 그 순간 로컬에서 검증한 것과 운영에서 도는 것이
    갈린다. 파일이 다른 것은 성질이 달라서다 — 코퍼스는 커밋되고 `.env`는 아니다.

트레이드오프:
    설정 오류가 컴파일 타임이 아니라 런타임에 드러난다. 이를 상쇄하기 위해
    로딩 시점에 전수 검증하고, 스키마를 어긴 파일은 부분 로딩 없이 통째로
    거부한다. 부분 로딩을 허용하면 "감시 대상 일부가 조용히 빠진" 상태가 되고,
    그것은 이 시스템에서 놓친 개정으로 직결된다.

엣지 케이스:
    - `law_id`가 6자리 숫자 문자열이 아니면 거부한다. 정수로 파싱하면 앞의 0이
      사라져 `009244`가 `9244`가 된다.
    - 활성 코퍼스가 하나도 없으면 거부한다. 감시 대상이 0건인 상태로 수집기가
      도는 것은 "그날 개정 없음"과 구별되지 않는다 (ADR-005의 R-11과 같은 계열).
    - 같은 `law_id`가 여러 코퍼스에 중복 등장하는 것은 허용한다. 코퍼스는 관점이며
      배타적 분할이 아니다. 다만 한 코퍼스 안의 중복은 오타로 보고 거부한다.
"""

from regchange.config.corpus import (
    AdmRule,
    Corpus,
    CorpusConfig,
    CorpusConfigError,
    LawRef,
    load_corpus_config,
)
from regchange.config.settings import (
    SettingsError,
    apply_dotenv,
    law_api_base_url,
    law_api_oc,
    snapshot_root,
)

__all__ = [
    "AdmRule",
    "Corpus",
    "CorpusConfig",
    "CorpusConfigError",
    "LawRef",
    "SettingsError",
    "apply_dotenv",
    "law_api_base_url",
    "law_api_oc",
    "load_corpus_config",
    "snapshot_root",
]
