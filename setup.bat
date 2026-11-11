@echo off
rem One-time setup for Windows: creates a private Python environment and installs everything.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv (echo Creating Python environment... & python -m venv .venv || (pause & exit /b 1))
echo Installing packages (this can take a few minutes)...
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\python -m pip install --quiet -r requirements.txt || (echo Installation failed. & pause & exit /b 1)
if not exist .env (copy .env.example .env >nul & echo Created .env - open it in Notepad and paste your Groq API key.)
echo.
echo Setup complete. Next: put your Groq key in .env, then double-click run.bat
pause