@echo off
REM One-command local edge stack on Windows: broker + edge service + store simulator.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo [setup] creating virtualenv
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -q -r requirements.txt || goto :err
)
set P=.venv\Scripts\python.exe
if not exist models\LATEST (
  if not exist data\daily_sales.csv.gz %P% -m retail_edge.data
  echo [setup] training model
  %P% -m retail_edge.train || goto :err
)
if not exist logs mkdir logs
start "mqtt-broker" /min cmd /c "%P% -m retail_edge.broker > logs\broker.log 2>&1"
timeout /t 2 >nul
start "edge-service" /min cmd /c "%P% -m retail_edge.service > logs\edge.log 2>&1"
timeout /t 6 >nul
start "store-simulator" /min cmd /c "%P% -m retail_edge.simulator --speed 4 > logs\simulator.log 2>&1"
echo Dashboard: http://localhost:8000   REST docs: http://localhost:8000/docs
start http://localhost:8000
echo Close the three minimised windows to stop.
goto :eof
:err
echo Setup failed. Is Python 3.10+ on PATH?
exit /b 1
