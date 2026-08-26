.DEFAULT_GOAL := help
.PHONY: help setup up down migrate test lint typecheck fmt check clean \
        ops-run ops-history ops-summary ops-alerts ops-install ops-uninstall \
        review-ui eval-impact eval-impact-deanchored eval-delegation eval-routing-precheck

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

ops-run: ## 일일 작업을 지금 한 번 돌린다 (cron 과 같은 경로)
	./scripts/ops/daily_ingest.sh

ops-history: ## 최근 30일 실행 이력
	uv run regchange ops history

ops-summary: ## 운영 시작일부터 오늘까지의 집계
	uv run regchange ops summary

ops-alerts: ## 최근 7일 알림 (MISMATCH · 변경규모 · 연속 0건 · 카나리아)
	uv run regchange ops alerts

review-ui: ## 검토 UI (http://127.0.0.1:8000) — 승인은 그래프 재개로만 이루어진다
	uv run regchange review serve

eval-impact: ## 영향평가 + gate 3단(anchored) 골든셋 측정 — 4단계 기준선
	uv run --group eval python -m evals.runners.impact_eval --model sonnet

eval-impact-deanchored: ## gate 3단을 de-anchored 로 돌려 anchored 와 대조한다
	uv run --group eval python -m evals.runners.impact_eval --model sonnet --grounding de-anchored

eval-delegation: ## 위임 승격 top_n 스윕 (R-22)
	uv run --group eval python -m evals.runners.delegation_sweep

eval-routing-precheck: ## 라우팅 사전 확인 — 기록과 검색만 읽는다 (LLM 미호출, $0)
	uv run --group eval python -m evals.runners.routing_precheck

ops-install: ## launchd 에 매일 07:00 KST 로 등록
	./scripts/ops/install_launchd.sh

ops-uninstall: ## launchd 등록 해제
	./scripts/ops/install_launchd.sh --uninstall

clean: ## 캐시 정리
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
