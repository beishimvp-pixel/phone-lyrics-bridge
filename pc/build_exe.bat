@echo off
cd /d "%~dp0"
echo Installing deps...
python -m pip install -r requirements.txt pyinstaller -q
echo Building exe...
python -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name "???" ^
  --hidden-import websockets ^
  --hidden-import httpx ^
  --hidden-import style_prefs ^
  --hidden-import hotkey ^
  --hidden-import lyrics ^
  --hidden-import qq_lyrics ^
  --hidden-import bili_subs ^
  --hidden-import netutil ^
  --collect-all websockets ^
  client.py
echo.
echo Done. EXE: dist\???.exe
