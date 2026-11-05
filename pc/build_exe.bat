@echo off
cd /d "%~dp0"
echo Installing deps...
python -m pip install -r requirements.txt
echo Building exe...
python -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name "歌词桥" ^
  --hidden-import websockets ^
  --hidden-import httpx ^
  --collect-all websockets ^
  client.py
echo.
echo Done. EXE: dist\歌词桥.exe
pause
