$ErrorActionPreference = "Stop"
$files = @(
  Get-ChildItem -Path "$PSScriptRoot\..\src" -Recurse -Include *.js
  Get-ChildItem -Path "$PSScriptRoot" -File -Include *.js
) | Select-Object -ExpandProperty FullName
foreach ($file in $files) {
  node --check $file | Out-Null
}
Write-Output "Syntax check passed for $($files.Count) files."
