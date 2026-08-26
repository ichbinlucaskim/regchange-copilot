"""de-anchored gate 3단의 경계를 고정한다 — **1단계에 무엇이 들어가고 무엇이 안 들어가는가.**

이 테스트가 존재하는 이유: de-anchoring 은 "검증기가 초안을 보기 전에 자기 답을 낸다"로만
성립한다. 1단계 프롬프트에 초안의 어떤 조각이라도 들어가면 그 성질이 조용히 사라지고,
사라진 것은 판정 분포로만 드러난다 — 즉 다음 측정 때까지 모른다.

경계는 "외부 입력인가, 우리 출력인가"다. 개정 조문은 법제처가 준 사실이라 들어가고,
의무사항은 우리 모델의 해석이라 들어가지 않는다.
"""

from __future__ import annotations

import pytest

from regchange.prompts.deanchored import (
    BLIND_SCHEMA,
    CONTRAST_SCHEMA,
    build_blind_content,
    build_contrast_content,
    parse_blind,
    parse_contrast,
)
from regchange.verification.base import RELATION_TO_LEVEL, ClaimRelation, SupportLevel
from regchange.verification.grounding import ClaimJudgment, decide

AMENDMENT = "제44조의4(다른 법률의 적용 배제) … 특별한 사유가 없으면 제공하여야 한다."
QUOTE = "제1항에도 불구하고 「개인정보 보호법」 제18조에서 정한 경우에는 동의 없이 제공할 수 있다."
SPEC = "ISP-GUIDE-003 제28조 (개인정보의 제3자 제공)"
CLAIM = "본 조의 승인 체계와 신설 조문의 의무적 제공 사이의 관계를 정리할 필요가 있다"


def test_blind_stage_gets_the_amendment() -> None:
    """1단계는 개정 조문을 본다 — **방향이 없으면 일반 요약이 되어 기준이 되지 못한다.**"""
    content, _ = build_blind_content(amendment=AMENDMENT, quote=QUOTE, spec=SPEC)

    assert AMENDMENT in content
    assert QUOTE in content
    assert SPEC in content


def test_blind_stage_never_sees_the_draft() -> None:
    """**1단계 입력에 초안의 주장이 들어갈 자리가 없다.**

    서명에 그 인자가 없다는 것이 이 성질의 실체다. 인자를 받으면서 "쓰지 않는다"로
    두면 다음 사람이 쓴다.
    """
    content, _ = build_blind_content(amendment=AMENDMENT, quote=QUOTE, spec=SPEC)

    assert CLAIM not in content
    assert "주장은 지금 보지 않는다" in content
    with pytest.raises(TypeError):
        build_blind_content(  # type: ignore[call-arg]
            amendment=AMENDMENT, quote=QUOTE, spec=SPEC, claim=CLAIM
        )


def test_blind_stage_refuses_empty_direction() -> None:
    """개정 조문 없이는 방향이 없다. 빈 값으로 부르지 않는다."""
    with pytest.raises(ValueError, match="개정 조문"):
        build_blind_content(amendment="   ", quote=QUOTE, spec=SPEC)


def test_blind_schema_has_no_verdict_field() -> None:
    """1단계는 **판정하지 않는다.** 판정 필드가 있으면 그 자리에서 초안 없이 판정하게 된다."""
    properties = BLIND_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"claimable", "limits"}
    assert BLIND_SCHEMA["additionalProperties"] is False


def test_blind_output_must_not_be_empty() -> None:
    """빈 기준은 무엇이든 통과시킨다. 파싱에서 막는다."""
    assert parse_blind({"claimable": "이 문단은 …", "limits": "…는 말할 수 없다"})
    with pytest.raises(ValueError, match="빈 기준"):
        parse_blind({"claimable": "   ", "limits": "x"})


def test_contrast_carries_the_blind_output_verbatim() -> None:
    """2단계는 1단계 출력을 **그대로** 싣는다. 다듬으면 초안 쪽으로 기울 수 있다."""
    claimable = "이 문단은 우리가 능동적으로 제공할 때의 동의 절차를 규율한다"
    limits = "지정 기관의 요청에 응하는 수동적 의무는 이 문단이 다루지 않는다"

    content, _ = build_contrast_content(
        claimable=claimable, limits=limits, claim=CLAIM, quote=QUOTE, spec=SPEC
    )

    assert claimable in content
    assert limits in content
    assert CLAIM in content


def test_contrast_does_not_carry_the_amendment() -> None:
    """2단계는 개정 조문을 다시 보지 않는다 — 1단계의 답이 그것을 대신한다."""
    content, _ = build_contrast_content(
        claimable="x", limits="y", claim=CLAIM, quote=QUOTE, spec=SPEC
    )

    assert AMENDMENT not in content


def test_contrast_schema_is_relational_not_gradual() -> None:
    """판정값이 관계 3종이다. 정도(`SupportLevel`)를 쓰지 않는다."""
    properties = CONTRAST_SCHEMA["properties"]
    assert isinstance(properties, dict)
    values = set(properties["relation"]["enum"])

    assert values == {r.value for r in ClaimRelation}
    assert "PARTIAL" not in values, "정도 어휘가 섞이면 약해진 주장이 위로 움직인다"


def test_unknown_relation_is_not_a_pass() -> None:
    """enum 밖의 값을 `WITHIN` 으로 떨어뜨리지 않는다. 검증 실패가 통과로 위장한다."""
    with pytest.raises(ValueError, match="MOSTLY"):
        parse_contrast({"relation": "MOSTLY", "reason": ""})


def test_relation_maps_to_decision_vocabulary() -> None:
    """`BEYOND` 와 `UNRELATED` 는 같은 결정이다 — 조치가 같기 때문이다."""
    assert RELATION_TO_LEVEL[ClaimRelation.WITHIN] is SupportLevel.SUPPORTED
    assert RELATION_TO_LEVEL[ClaimRelation.BEYOND] is SupportLevel.UNSUPPORTED
    assert RELATION_TO_LEVEL[ClaimRelation.UNRELATED] is SupportLevel.UNSUPPORTED
    assert set(RELATION_TO_LEVEL) == set(ClaimRelation), (
        "사상되지 않는 관계가 있으면 KeyError 가 난다"
    )


def test_relation_counts_are_kept_alongside_the_decision() -> None:
    """사상된 뒤에도 원본 관계가 남는다 — 측정이 `BEYOND` 와 `UNRELATED` 를 구별해야 한다."""
    result = decide(
        [
            ClaimJudgment(
                key="impact:0",
                level=SupportLevel.UNSUPPORTED,
                reason="",
                relation=ClaimRelation.BEYOND,
            ),
            ClaimJudgment(
                key="dept:0",
                level=SupportLevel.UNSUPPORTED,
                reason="",
                relation=ClaimRelation.UNRELATED,
            ),
        ]
    )

    assert result.counts[SupportLevel.UNSUPPORTED] == 2
    assert result.relation_counts[ClaimRelation.BEYOND] == 1
    assert result.relation_counts[ClaimRelation.UNRELATED] == 1
    assert result.needs_rewrite is True


def test_anchored_judgments_have_no_relation() -> None:
    """anchored 실행에서는 관계 집계가 전부 0이다 — 어느 검증기로 돌았는지가 드러난다."""
    result = decide([ClaimJudgment(key="impact:0", level=SupportLevel.PARTIAL, reason="")])

    assert set(result.relation_counts) == set(ClaimRelation)
    assert sum(result.relation_counts.values()) == 0
