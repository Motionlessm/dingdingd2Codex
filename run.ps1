$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python .\app.py --host 127.0.0.1 --port 8787
