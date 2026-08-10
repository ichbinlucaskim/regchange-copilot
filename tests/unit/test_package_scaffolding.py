"""스캐폴딩이 CLAUDE.md §3(4항목 docstring)을 만족하는지 검사한다.

이 테스트가 존재하는 이유: ruff 의 D 룰셋은 docstring 의 "존재"만 검사할 수 있고
4항목이 채워졌는지는 보지 못한다. 새 모듈이 빈 docstring 하나로 린트를 통과하는
것을 막기 위해 모듈 수준에서만이라도 기계적으로 강제한다. 함수·클래스 수준의
충족 여부는 여전히 코드 리뷰가 판정한다.
"""

import ast
from pathlib import Path

import pytest

import regchange

SRC_ROOT = Path(regchange.__file__).parent

REQUIRED_SECTIONS = ("목적:", "구현 이유:", "트레이드오프:", "엣지 케이스:")

PYTHON_MODULES = sorted(SRC_ROOT.rglob("*.py"))


def test_source_tree_is_not_empty() -> None:
    """스캐폴딩 자체가 사라졌는데 아래 테스트들이 0건 통과하는 것을 막는다."""
    assert PYTHON_MODULES, f"{SRC_ROOT} 아래에 파이썬 모듈이 없다"


@pytest.mark.parametrize("module_path", PYTHON_MODULES, ids=lambda p: p.name)
def test_module_has_four_section_docstring(module_path: Path) -> None:
    """모든 모듈 docstring은 4항목(목적/구현 이유/트레이드오프/엣지 케이스)을 갖는다."""
    docstring = ast.get_docstring(ast.parse(module_path.read_text(encoding="utf-8")))
    relative = module_path.relative_to(SRC_ROOT.parent)

    assert docstring, f"{relative}: 모듈 docstring 이 없다"

    missing = [section for section in REQUIRED_SECTIONS if section not in docstring]
    assert not missing, f"{relative}: docstring 에 {missing} 항목이 없다 (CLAUDE.md §3)"


def test_every_package_directory_is_importable() -> None:
    """src/regchange 아래 모든 디렉터리가 __init__.py 를 갖는다 (네임스페이스 패키지 금지)."""
    missing = [
        directory.relative_to(SRC_ROOT.parent)
        for directory in SRC_ROOT.rglob("*")
        if directory.is_dir()
        and directory.name != "__pycache__"
        and not (directory / "__init__.py").exists()
    ]
    assert not missing, f"__init__.py 가 없는 디렉터리: {missing}"
