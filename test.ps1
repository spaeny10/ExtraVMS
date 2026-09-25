# Run every backend test suite and the frontend build. Exit code 1 if anything fails.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"
$failed = @()
Get-ChildItem "$root\backend\tests\test_*.py" | ForEach-Object {
    Write-Host "== $($_.Name)"
    Push-Location "$root\backend"
    & $py $_.FullName 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { $failed += $_.Name }
    Pop-Location
}
Write-Host "== frontend build"
Push-Location "$root\frontend"
npm run build 2>&1 | Select-String -Pattern "error|built" | ForEach-Object { $_.Line }
if ($LASTEXITCODE -ne 0) { $failed += "frontend build" }
Pop-Location
if ($failed.Count) { Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "all passed" -ForegroundColor Green
