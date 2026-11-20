@echo off
cd /d "C:\Users\VISHEK\.gemini\antigravity\scratch\day-trader-workspace"
git pull origin main >nul 2>&1
start "" http://localhost:8501
py -m streamlit run dashboard.py --server.port 8501 --server.headless true
