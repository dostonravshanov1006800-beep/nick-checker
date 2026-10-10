@echo off
REM Запуск локального сервиса: Windows (спека §10.2)
cd /d %~dp0
if not exist .env (echo Скопируйте .env.example в .env и заполните & exit /b 1)
for /f "eol=# delims=" %%i in (.env) do set "%%i"
python -m pip install -q -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
