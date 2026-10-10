#!/usr/bin/env bash
# Запуск локального сервиса: Linux и macOS (спека §10.2)
cd "$(dirname "$0")"
[ -f .env ] || { echo "Скопируйте .env.example в .env и заполните"; exit 1; }
export $(grep -v '^#' .env | grep -v '^$' | xargs)
python3 -m pip install -q -r requirements.txt 2>/dev/null || pip install -q -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
