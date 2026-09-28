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
Write-Host "== frontend URL prefix check"
$stray = Get-ChildItem "$root\frontend\src" -Recurse -Include *.ts,*.tsx | Where-Object { $_.Name -ne "api.ts" } |
    Select-String -Pattern '["`]/api/' | ForEach-Object { "$($_.Filename):$($_.LineNumber)" }
if ($stray) { Write-Host "backend URLs must go through BASE (api.ts): $($stray -join ', ')"; $failed += "frontend URL prefix" } else { Write-Host "ok" }
Write-Host "== frontend unit tests"
Push-Location "$root\frontend"
npm test -- --reporter=dot 2>&1 | Select-String -Pattern "Tests|Test Files|FAIL|Error" | ForEach-Object { $_.Line }
if ($LASTEXITCODE -ne 0) { $failed += "frontend tests" }
Write-Host "== frontend build"
npm run build 2>&1 | Select-String -Pattern "error|built" | ForEach-Object { $_.Line }
if ($LASTEXITCODE -ne 0) { $failed += "frontend build" }
Pop-Location
Write-Host "== hub tests"
Push-Location "$root\hub"
& $py -m pytest -q tests 2>&1 | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) { $failed += "hub tests" }
Pop-Location
Write-Host "== hub UI build"
Push-Location "$root\hub\ui"
npm run build 2>&1 | Select-String -Pattern "error|built" | ForEach-Object { $_.Line }
if ($LASTEXITCODE -ne 0) { $failed += "hub UI build" }
Pop-Location
if ($failed.Count) { Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "all passed" -ForegroundColor Green
