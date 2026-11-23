# Ashie installer for Windows. Run from the repo folder:  powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
$py = $null
foreach ($v in "3.11", "3.12", "3.10") { & py "-$v" --version 2>$null; if ($LASTEXITCODE -eq 0) { $py = $v; break } }
if (-not $py) { Write-Host "Install Python 3.11 from python.org first (3.13 is not supported)." -ForegroundColor Red; exit 1 }
Write-Host "Using Python $py"
py "-$py" -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install webrtcvad-wheels
& .\.venv\Scripts\pip.exe install resemblyzer --no-deps
& .\.venv\Scripts\pip.exe install -r requirements.txt
& .\.venv\Scripts\pip.exe install -U edge-tts
& .\.venv\Scripts\python.exe -c "from resemblyzer import VoiceEncoder; print('Voice lock OK')"
Write-Host "Done. Next:  .venv\Scripts\activate   then   python ashie.py --setup" -ForegroundColor Green
