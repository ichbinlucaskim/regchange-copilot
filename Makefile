.DEFAULT_GOAL := help
.PHONY: help setup up down migrate test lint typecheck fmt check clean

help: ## 사용 가능한 타깃을 출력한다
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## 의존성 설치 (uv sync) 및 .env 생성
	uv sync --all-groups
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

up: ## Postgres + pgvector 기동
	docker compose up -d
	@echo "waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U regchange -d regchange >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready"

down: ## 컨테이너 종료 (볼륨은 유지)
	docker compose down

migrate: ## db/migrations 를 순서대로 적용 (적용된 것은 건너뛴다)
	uv run python -m regchange.store

test: ## 테스트 실행
	uv run pytest

lint: ## ruff + import-linter 검사 (수정하지 않음)
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports

fmt: ## ruff 포맷 + 자동 수정
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy strict
	uv run mypy

check: lint typecheck test ## lint + typecheck + test

clean: ## 캐시 정리
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
