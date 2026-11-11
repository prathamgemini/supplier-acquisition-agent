@echo off
rem Starts the dashboard and opens it in your browser. Close this window to stop the app.
cd /d "%~dp0"
if not exist .venv (echo Run setup.bat first. & pause & exit /b 1)
.venv\Scripts\python -m streamlit run app.py
pause