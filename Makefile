.PHONY: install dev test lint docker

install:
	python -m pip install -e ".[dev]"

dev:
	uvicorn app.main:app --reload

test:
	pytest

lint:
	ruff check .

docker:
	docker compose up --build
