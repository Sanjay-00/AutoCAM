@echo off
cd /d "%~dp0"
start "" http://localhost:8501
cibil\Scripts\python.exe -m streamlit run app.py
pause
