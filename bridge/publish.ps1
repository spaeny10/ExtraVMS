# Publish an Axiom Bridge release to the hub's /downloads/bridge/ (hub/docker-compose.yml mounts hub/downloads there).
# Builds with build.ps1, uploads dist\* to <hub>:/opt/axiom/hub/downloads/bridge/<version>/ and then replaces
# latest.json, which the Site page's enrollment dialog reads for its one-line install (download + SHA-256 + install).
# A version's files never change once published (the hub serves them as immutable): bump -Version for a new build.
#
#   .\publish.ps1 -Version 0.1.1 -Key C:\path\to\ssh-key [-HubHost root@178.156.211.145]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Key,
    [string]$HubHost = "root@178.156.211.145",
    [string]$RemoteDir = "/opt/axiom/hub/downloads/bridge",
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $SkipBuild) { & .\build.ps1 -Version $Version }
$files = @("axiom-bridge.exe", "axiom-bridge-linux-amd64", "axiom-bridge-linux-arm64")
foreach ($f in $files) { if (-not (Test-Path "dist\$f")) { throw "dist\$f is missing: build first" } }
$sha = @{}
foreach ($f in $files) { $sha[$f] = (Get-FileHash "dist\$f" -Algorithm SHA256).Hash.ToLower() }

$ssh = @("-i", $Key, "-o", "BatchMode=yes")
& ssh @ssh $HubHost "test ! -e $RemoteDir/$Version || { echo 'version $Version is already published: bump -Version'; exit 3; }; mkdir -p $RemoteDir/$Version"
if ($LASTEXITCODE -ne 0) { throw "refused (exit $LASTEXITCODE)" }
foreach ($f in $files) { & scp @ssh "dist\$f" "${HubHost}:$RemoteDir/$Version/$f"; if ($LASTEXITCODE -ne 0) { throw "upload of $f failed" } }

# latest.json last, so the dialog never offers a version whose files aren't all there yet
$latest = [ordered]@{
    version      = $Version
    published_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    windows      = [ordered]@{ file = "$Version/axiom-bridge.exe"; sha256 = $sha["axiom-bridge.exe"] }
    linux_amd64  = [ordered]@{ file = "$Version/axiom-bridge-linux-amd64"; sha256 = $sha["axiom-bridge-linux-amd64"] }
    linux_arm64  = [ordered]@{ file = "$Version/axiom-bridge-linux-arm64"; sha256 = $sha["axiom-bridge-linux-arm64"] }
}
$tmp = Join-Path $env:TEMP "axiom-bridge-latest.json"
($latest | ConvertTo-Json -Depth 4) | Set-Content -Encoding ascii $tmp
& scp @ssh $tmp "${HubHost}:$RemoteDir/latest.json.new"
& ssh @ssh $HubHost "mv $RemoteDir/latest.json.new $RemoteDir/latest.json && chmod -R a+rX $RemoteDir"
if ($LASTEXITCODE -ne 0) { throw "latest.json update failed" }
Remove-Item $tmp
Write-Host "published Axiom Bridge $Version (windows sha256 $($sha['axiom-bridge.exe']))"
