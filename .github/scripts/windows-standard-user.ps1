# One step of the native-Windows check, run as a standard (non-admin) local
# account (.github/workflows/windows-native.yml starts it with
# Start-Process -Credential). GitHub's Windows runners are administrators
# with UAC off, so everything else in that workflow runs elevated; this is
# the plugin as a person's ordinary account runs it.
#
#   -Step whoami        prove the account is not an administrator
#   -Step tests         the plugin's test suite (lock, access lists, key file)
#   -Step hermes        install Hermes for this account (install.ps1)
#   -Step gateway       the plugin from -Sha, a login to the stand-in API on
#                       -Port, the gateway's leader lock held, then free after
#                       a stop and after a kill
#   -Step refused       a key directory another account owns is not written into
#   -Step denied        what a key file this account cannot read says, and its remedy
#
# The probes (windows-check.py) run with -Python, the runner's interpreter
# with this package installed. Exit 0 when the step holds.

param(
    [Parameter(Mandatory)] [string] $Step,
    [Parameter(Mandatory)] [string] $Python,
    [Parameter(Mandatory)] [string] $Repo,
    [string] $Channel = 'stable',
    [string] $Sha = '',
    [string] $Source = '',
    [int] $Port = 0,
    [string] $KeyHome = ''
)

$ErrorActionPreference = 'Stop'
# Start-Process -Credential hands this process the caller's environment
# (USERPROFILE, LOCALAPPDATA, TEMP of the administrator account), and the
# shell folders expand %USERPROFILE% from it. The account's own folders, from
# its profile as Windows registered it (-LoadUserProfile made it), as a logon
# would set them:
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$profileDir = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid").ProfileImagePath
$env:USERPROFILE = $profileDir
$env:HOME = $profileDir
$env:LOCALAPPDATA = Join-Path $profileDir 'AppData\Local'
$env:APPDATA = Join-Path $profileDir 'AppData\Roaming'
$env:USERNAME = [Environment]::UserName
$env:TEMP = Join-Path $env:LOCALAPPDATA 'Temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$check = Join-Path $Repo '.github\scripts\windows-check.py'
$hermesHome = Join-Path $env:LOCALAPPDATA 'hermes'
$hermes = Join-Path $hermesHome 'bin\hermes.exe'
if (-not (Test-Path $hermes)) { $hermes = Join-Path $hermesHome 'bin\hermes.cmd' }

function Assert-Exit([string] $What) {
    if ($LASTEXITCODE -ne 0) { throw "$What exited $LASTEXITCODE" }
}

switch ($Step) {
    'whoami' {
        whoami /user /groups
        $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw "this account is an administrator"
        }
        Write-Output "standard account, not an administrator: $($env:USERNAME), LOCALAPPDATA=$($env:LOCALAPPDATA), TEMP=$($env:TEMP)"
    }
    'tests' {
        Push-Location $Repo
        try {
            & $Python -m pytest -q -ra -p no:cacheprovider tests
            Assert-Exit 'pytest'
        } finally { Pop-Location }
    }
    'hermes' {
        $script = (Invoke-RestMethod https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1).TrimStart([char]0xFEFF)
        & ([scriptblock]::Create($script)) -NonInteractive -Channel $Channel
        if (-not (Test-Path (Join-Path $hermesHome 'bin'))) { throw "no Hermes launcher under $hermesHome" }
        $hermes = Join-Path $hermesHome 'bin\hermes.exe'
        if (-not (Test-Path $hermes)) { $hermes = Join-Path $hermesHome 'bin\hermes.cmd' }
        & $hermes --version
        Assert-Exit 'hermes --version'
    }
    'gateway' {
        $env:HERMES_NONINTERACTIVE = '1'
        $env:HERMES_GATEWAY_INSTALL_START_ON_LOGIN = '0'
        & $hermes plugins install $Source --ref $Sha --force --yes-deps
        Assert-Exit 'plugins install'
        & $hermes plugins enable agentchat
        Assert-Exit 'plugins enable'
        $env:AGENTCHATME_API_BASE = "http://127.0.0.1:$Port"
        # A made-up key of the real shape, made here and never stored.
        & $Python -c "import secrets; print('ac_' + secrets.token_urlsafe(32))" | & $hermes agentchat login --api-key -
        Assert-Exit 'login'
        Remove-Item Env:AGENTCHATME_API_BASE
        Write-Output "--- the key directory's access list"
        icacls (Join-Path $hermesHome 'agentchat')
        & $hermes gateway start
        & $Python $check leader $hermesHome 300
        Assert-Exit 'the leader check'
        $record = Get-ChildItem (Join-Path $hermesHome 'agentchat-locks') -Filter 'ws-*.health.json' | Select-Object -First 1
        $holder = (Get-Content $record.FullName | ConvertFrom-Json).pid
        Get-CimInstance Win32_Process -Filter "ProcessId = $holder" | Select-Object ProcessId, CommandLine | Format-List
        Get-ChildItem (Join-Path $hermesHome 'logs') -Filter *.log | Select-String -Pattern 'agentchat runtime started' | Select-Object -Last 3
        & $hermes gateway stop
        & $Python $check free $hermesHome 60
        Assert-Exit 'the free check after a stop'
        & $hermes gateway start
        & $Python $check leader $hermesHome 300
        Assert-Exit 'the leader check'
        $holder = (Get-Content $record.FullName | ConvertFrom-Json).pid
        Write-Output "killing the gateway, pid $holder"
        Stop-Process -Id $holder -Force
        & $Python $check free $hermesHome 60
        Assert-Exit 'the free check after a kill'
    }
    'refused' {
        & $Python $check write-refused $KeyHome 'another account'
        Assert-Exit 'the refused write'
    }
    'denied' {
        & $Python $check refusal $KeyHome 'Access is denied'
        Assert-Exit 'the denied read'
        & $Python $check refusal $KeyHome 'doctor --fix` from an elevated (Run as administrator) prompt'
        Assert-Exit 'the elevated remedy'
    }
    default { throw "unknown step $Step" }
}
exit 0
