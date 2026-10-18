# Build the Axiom Bridge: static binaries (no cgo) for Windows x64 and Linux x64/arm64 into bridge\dist.
#   .\build.ps1                 version 0.1.0+<git commit>
#   .\build.ps1 -Version 0.2.0  an explicit version
#   .\build.ps1 -Test           run go vet and go test first
param(
    [string]$Version = "",
    [switch]$Test
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$saved = @{ CGO_ENABLED = $env:CGO_ENABLED; GOOS = $env:GOOS; GOARCH = $env:GOARCH }
try {
    if (-not $Version) {
        $commit = (git rev-parse --short HEAD 2>$null)
        $Version = if ($commit) { "0.1.0+$commit" } else { "0.1.0" }
    }
    if ($Test) {
        go vet ./...
        if ($LASTEXITCODE -ne 0) { throw "go vet failed" }
        go test ./...
        if ($LASTEXITCODE -ne 0) { throw "go test failed" }
    }
    New-Item -ItemType Directory -Force dist | Out-Null
    $env:CGO_ENABLED = "0"
    $targets = @(
        @{ os = "windows"; arch = "amd64"; out = "dist\axiom-bridge.exe" },
        @{ os = "linux";   arch = "amd64"; out = "dist\axiom-bridge-linux-amd64" },
        @{ os = "linux";   arch = "arm64"; out = "dist\axiom-bridge-linux-arm64" }
    )
    foreach ($t in $targets) {
        $env:GOOS = $t.os
        $env:GOARCH = $t.arch
        go build -trimpath -ldflags "-s -w -X main.version=$Version" -o $t.out ./cmd/axiom-bridge
        if ($LASTEXITCODE -ne 0) { throw "build failed for $($t.os)/$($t.arch)" }
        $size = [math]::Round((Get-Item $t.out).Length / 1MB, 1)
        Write-Host ("{0,-34} {1,5} MB  {2}/{3}" -f $t.out, $size, $t.os, $t.arch)
    }
    Get-FileHash dist\axiom-bridge* -Algorithm SHA256 | ForEach-Object { "{0}  {1}" -f $_.Hash.ToLower(), (Split-Path $_.Path -Leaf) } |
        Set-Content -Encoding ascii dist\SHA256SUMS.txt
    Write-Host "version $Version; checksums in dist\SHA256SUMS.txt"
}
finally {
    foreach ($k in $saved.Keys) { Set-Item "env:$k" $saved[$k] -ErrorAction SilentlyContinue; if ($null -eq $saved[$k]) { Remove-Item "env:$k" -ErrorAction SilentlyContinue } }
    Pop-Location
}
