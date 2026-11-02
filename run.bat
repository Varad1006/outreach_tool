@echo off
REM One-command start on Windows.
cd /d %~dp0
if not exist .venv python -m venv .venv
.venv\Scripts\pip install -q -r requirements.txt
if not exist .env (copy .env.example .env & echo Created .env - fill in your keys, then run run.bat again. & exit /b)
.venv\Scripts\python app.py
