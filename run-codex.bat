@echo off
setlocal
cd /d "%~dp0"

set "CODEX=C:\Users\user\.vscode\extensions\openai.chatgpt-26.5415.20818\bin\windows-x86_64\codex.exe"

"%CODEX%" --sandbox danger-full-access --ask-for-approval on-request %*

echo.
pause