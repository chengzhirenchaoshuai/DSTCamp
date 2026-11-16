@echo off
rem Same-feature mod list review tool: opens the review page in the browser
cd /d "%~dp0.."
python scripts\mod_conflict_tool.py
if errorlevel 1 pause
