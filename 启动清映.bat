@echo off
cd /d "%~dp0"
if exist "%~dp0dist\清映\清映.exe" (
  start "" "%~dp0dist\清映\清映.exe"
  exit /b 0
)
py -3 -c "import PySide6.QtWidgets" >nul 2>nul
if errorlevel 1 (
  echo 正在安装界面组件，第一次会稍慢…
  py -3 -m pip install -r "%~dp0requirements.txt"
)
for /f "delims=" %%i in ('py -3 -c "import pathlib,sys; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"') do set "PYW=%%i"
if exist "%PYW%" (
  start "" "%PYW%" "%~dp0main.py"
) else (
  start "" py -3 "%~dp0main.py"
)
