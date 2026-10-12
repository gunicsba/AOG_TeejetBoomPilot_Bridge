@echo off
rem Builds dist\AOG-TeejetBoomPilot.exe (GUI, no console window)
python -m pip install -q python-can pyinstaller
python -m PyInstaller --noconfirm --onefile --noconsole --name AOG-TeejetBoomPilot ^
  --collect-submodules can.interfaces --add-data "teejet_bridge/lang;teejet_bridge/lang" ^
  teejet_bridge_app.py
