# First-run setup; current-user local runtime, no admin and no PATH changes.
param([switch]$SetupOnly, [switch]$ExistingPythonOnly)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
Set-Location $PSScriptRoot
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
$runtime = Join-Path $PSScriptRoot 'runtime'
$venv = Join-Path $PSScriptRoot '.venv'
$vpy = Join-Path $venv 'Scripts\python.exe'
$cache = Join-Path $PSScriptRoot 'setup_cache'
New-Item -ItemType Directory -Force -Path $cache | Out-Null

function Test-CompatiblePython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    try {
        & $Candidate -c "import sys; raise SystemExit(0 if (3,11)<=sys.version_info[:2]<=(3,13) and sys.maxsize>2**32 else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

if (Test-Path -LiteralPath $vpy) {
    if (-not (Test-CompatiblePython $vpy)) { throw 'The project .venv is incompatible. Rename .venv and rerun setup; keep workspace data.' }
} else {
    $candidates = @((Join-Path $runtime 'python.exe'))
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidates += $pythonCommand.Source }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $listed = & $launcher.Source -0p 2>$null
            foreach ($entry in $listed) {
                if ($entry -match '([A-Za-z]:\\.+python\.exe)\s*$') { $candidates += $Matches[1] }
            }
        } catch { Write-Host 'Python launcher discovery unavailable; checking known candidates.' }
    }
    $py = $null
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-CompatiblePython $candidate) { $py = $candidate; break }
    }
    if (-not $py -and $ExistingPythonOnly) { throw 'No supported 64-bit Python 3.11-3.13 found. Use the main launcher to install Python.' }
    if (-not $py) {
    $py = Join-Path $runtime 'python.exe'
    Write-Host 'First setup downloads official Python 3.13.15 (about 28 MB). Research data is NOT uploaded.'
    $exe = Join-Path $cache 'python-3.13.15-amd64.exe'
    $url = 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    $expected = 'edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403'
    if ((Get-FileHash -Algorithm SHA256 $exe).Hash.ToLower() -ne $expected) { throw 'Python installer hash mismatch; installation stopped.' }
    $argsInstall = "/quiet InstallAllUsers=0 TargetDir=`"$runtime`" Include_pip=1 Include_test=0 Include_doc=0 Include_launcher=0 PrependPath=0 AppendPath=0 Shortcuts=0 AssociateFiles=0"
    $p = Start-Process -FilePath $exe -ArgumentList $argsInstall -WindowStyle Hidden -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "Python installer failed: $($p.ExitCode)" }
    if (-not (Test-Path $py)) { throw 'Python runtime was not created. See docs for existing Python installation.' }
    }
    if (-not (Test-CompatiblePython $py)) { throw 'Python runtime validation failed.' }
    Write-Host "Creating isolated environment with $py"
    & $py -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create isolated Python environment.' }
}
Write-Host 'Checking the frozen installation tool...'
& $vpy -B -m pip install --disable-pip-version-check --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r (Join-Path $PSScriptRoot 'installer.lock')
if ($LASTEXITCODE -ne 0) { throw 'Frozen installer bootstrap failed.' }
Write-Host 'Installing pinned calculation packages from PyPI. Internet is needed only for setup.'
& $vpy -m pip install --disable-pip-version-check --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r (Join-Path $PSScriptRoot 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Dependency download failed. Check network; data and prior results are unchanged.' }
Write-Host 'Running local tests before startup...'
& $vpy -B -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Local tests failed. Please send validation log to developer.' }
if ($SetupOnly) { Write-Host 'Setup and local tests passed.'; exit 0 }
& $vpy -B app.py serve
exit $LASTEXITCODE
