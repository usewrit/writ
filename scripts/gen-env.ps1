# gen-env.ps1 — create a filled-in .env from .env.example with fresh secrets.
#
#   .\scripts\gen-env.ps1            # refuses to overwrite an existing .env
#   .\scripts\gen-env.ps1 -Force     # overwrite an existing .env
#
# The Windows twin of scripts/gen-env.sh. It exists because that script is POSIX
# sh and needs `openssl` and `sed`, so on a stock Windows box the documented
# first step of the quickstart simply cannot run — and telling people to install
# Git Bash to generate a config file is not a quickstart.
#
# Generates the same eight values, and the list MUST stay in sync with both
# gen-env.sh and the startup validator in coordinator/config.py
# (_validate_production_secrets). The self-check at the end enforces that: if the
# template ever loses a key, this fails loudly instead of writing a .env that
# cannot boot.

[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path -Parent $PSScriptRoot
$EnvExample = Join-Path $Root '.env.example'
$EnvFile    = Join-Path $Root '.env'

if (-not (Test-Path -LiteralPath $EnvExample)) {
    Write-Error "$EnvExample not found."
}
if ((Test-Path -LiteralPath $EnvFile) -and -not $Force) {
    Write-Error @"
$EnvFile already exists - refusing to overwrite it.
       Re-run with -Force to replace it (your current secrets will be lost).
"@
}

# --- Secret generation -------------------------------------------------------
# .NET's cryptographic RNG, not Get-Random: Get-Random is a seeded PRNG and is
# explicitly not suitable for key material.
function New-HexSecret {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
}

# SECRET_ENCRYPTION_KEY must be a Fernet key: 32 random bytes, urlsafe-base64.
# Same construction as gen-env.sh's openssl fallback - standard base64 with the
# two URL-unsafe characters swapped. The '=' padding is part of a valid Fernet
# key and is kept.
function New-FernetKey {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
}

$secrets = [ordered]@{
    WRIT_JWT_SECRET       = New-HexSecret
    API_SECRET_KEY        = New-HexSecret
    HMAC_SECRET_KEY       = New-HexSecret
    RECORDER_AUTH_SECRET  = New-HexSecret
    INTERNAL_API_SECRET   = New-HexSecret
    GATEWAY_SECRET        = New-HexSecret
    # The document/OCR service and every agent that calls it share this one.
    # Generated here so PDF and scanned-page coverage is simply on after the
    # quickstart rather than a lane the operator has to discover and wire up.
    DOC_EXTRACT_SECRET    = New-HexSecret
    SECRET_ENCRYPTION_KEY = New-FernetKey
}

# --- Write .env ---------------------------------------------------------------
# Read as lines and rebuild with "`n". Windows would otherwise produce CRLF, and
# Docker Compose's env_file parser treats the trailing CR as part of the VALUE:
# every secret silently gains an invisible \r, the Fernet key stops being valid
# base64, and the coordinator fails to decrypt anything with an error that points
# nowhere near this file. `git config core.autocrlf true` can also have already
# put CRLF into .env.example, so strip rather than assume.
$lines = [System.IO.File]::ReadAllText($EnvExample) -split "`r?`n"

$out = foreach ($line in $lines) {
    $replaced = $line
    foreach ($name in $secrets.Keys) {
        if ($line -match "^$([regex]::Escape($name))=") {
            $replaced = "$name=$($secrets[$name])"
            break
        }
    }
    $replaced
}

[System.IO.File]::WriteAllText($EnvFile, ($out -join "`n"))

# --- Self-check: every required secret actually landed -------------------------
# The substitution above only fires on an existing `NAME=` line. If the template
# loses one, it silently does nothing - so verify rather than trust.
$written  = [System.IO.File]::ReadAllText($EnvFile)
$missing  = @()
foreach ($name in $secrets.Keys) {
    if ($written -notmatch "(?m)^$([regex]::Escape($name))=.+") { $missing += $name }
}
if ($missing.Count -gt 0) {
    Remove-Item -LiteralPath $EnvFile -Force
    Write-Error @"
these required secrets are missing from .env.example, so they could not be
       generated: $($missing -join ' ')
       Add them to .env.example (as bare 'NAME=' lines) and re-run.
"@
}

# --- Lock the file down --------------------------------------------------------
# The Windows equivalent of `chmod 600`: drop inherited ACLs and grant the
# current user alone. The file holds live signing keys, and on a shared or
# domain-joined machine the inherited ACL is frequently wider than the owner.
try {
    $acl = Get-Acl -LiteralPath $EnvFile
    $acl.SetAccessRuleProtection($true, $false)   # protected, do not copy inherited rules
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $me, 'FullControl', 'None', 'None', 'Allow')))
    Set-Acl -LiteralPath $EnvFile -AclObject $acl
} catch {
    Write-Warning "Could not restrict permissions on .env - it holds live signing keys, so tighten them by hand. ($_)"
}

Write-Host "Wrote $EnvFile with freshly generated secrets."
Write-Host ""
Write-Host "IMPORTANT:"
Write-Host "  * BACK UP SECRET_ENCRYPTION_KEY somewhere separate from your data volume."
Write-Host "    It encrypts stored credentials at rest; without it a database backup"
Write-Host "    cannot decrypt them. See SECURITY.md."
Write-Host "  * For production, edit .env and set WRIT_PUBLIC_URL to your public"
Write-Host "    https:// URL and ALLOWED_HOSTS to the hostname(s) you serve on."
Write-Host ""
Write-Host "Next: docker compose up -d --build"
