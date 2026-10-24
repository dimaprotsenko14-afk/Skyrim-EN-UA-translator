@echo off
title Skyrim SE/AE English-to-Ukrainian Mod Translator GUI
cd /d "%~dp0"

echo ===============================================================================
echo  Launching Skyrim SE/AE Ukrainian Mod Translator Desktop GUI...
echo ===============================================================================
echo.

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -u skyrim_translator_gui.py
) else (
    python -u skyrim_translator_gui.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Application exited with error code %ERRORLEVEL%.
    pause
)
