@echo off
REM Εκκίνηση εφαρμογής αποθήκης (Windows). Διπλό κλικ σε αυτό το αρχείο.
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python run.py
pause
