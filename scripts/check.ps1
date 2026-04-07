$ErrorActionPreference = "Stop"
python -m compileall "$PSScriptRoot\..\motionxbot" "$PSScriptRoot\..\main.py" | Out-Null
Write-Output "Python compile check passed."
