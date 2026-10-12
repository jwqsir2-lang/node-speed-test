@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\pythonw.exe" gui.py
