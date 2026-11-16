.PHONY: up down logs ps smoke test lint traffic traffic-stop traffic-logs

up:
	docker compose up --build -d --wait

down:
	docker compose --profile traffic down

logs:
	docker compose logs -f api

ps:
	docker compose ps

smoke:
	docker compose exec -T redis redis-cli DEL lab:product:1
	docker compose exec -T api python - < scripts/smoke.py

test:
	uv run --locked pytest -q

lint:
	uv run --locked ruff check .

traffic:
	docker compose --profile traffic up -d loadgen

traffic-stop:
	docker compose --profile traffic stop loadgen

traffic-logs:
	docker compose --profile traffic logs -f loadgen

.PHONY: fault-latency fault-errors fault-redis recover compare status check-rules
fault-latency:
	python3 scripts/lab.py latency
fault-errors:
	python3 scripts/lab.py errors
fault-redis:
	python3 scripts/lab.py redis
recover:
	python3 scripts/lab.py recover
status:
	python3 scripts/lab.py status
compare: traffic-stop
	python3 scripts/lab.py compare --seconds 30
check-rules:
	docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
	docker compose exec -T -w /etc/prometheus prometheus promtool test rules rules.test.yml
	docker compose exec -T -w /etc/prometheus prometheus promtool test rules slo.test.yml

.PHONY: fault-cpu
fault-cpu:
	python3 scripts/lab.py cpu --seconds 60

.PHONY: simulate test-console
simulate:
	uv run --locked python -m scripts.simulate
test-console:
	node --test tests/console.test.cjs

.PHONY: runtime-status runtime-rollback runtime-events runtime-healing
runtime-status:
	python3 -m ops.deploy status
runtime-rollback:
	python3 -m ops.deploy rollback
runtime-events:
	tail -n 30 runtime/events.jsonl
runtime-healing:
	python3 -m ops.heal status
