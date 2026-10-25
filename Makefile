.PHONY: venv install run test background stop backup restore

HOST := 127.0.0.1
PORT ?= 8000

venv:
	python3 -m venv .venv

install: venv
	.venv/bin/pip install -r requirements.txt

run:
	.venv/bin/python -m uvicorn app.main:app --host $(HOST) --port $(PORT)

test:
	.venv/bin/python -m pytest

background:
	scripts/run-background.sh

stop:
	scripts/stop.sh

backup:
	.venv/bin/python -m app.backup backup

restore:
	@test -n "$(FILE)" || (echo "Usage: make restore FILE=data/backups/studio-YYYYMMDDTHHMMSSZ.db" && exit 2)
	.venv/bin/python -m app.backup restore $(FILE)
