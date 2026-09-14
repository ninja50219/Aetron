@echo off
cd /d "%~dp0"
python -m aetron ui
if errorlevel 1 pause
