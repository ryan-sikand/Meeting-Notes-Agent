param()

$ErrorActionPreference = "Stop"

function ConvertTo-PlainText {
    param([Security.SecureString]$SecureValue)

    if ($null -eq $SecureValue) {
        return ""
    }

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function ConvertTo-DotEnvValue {
    param([string]$Value)

    $escaped = $Value.Replace("\", "\\").Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Set-DotEnvValue {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [string]$Name,
        [string]$Value
    )

    $replacement = $Name + "=" + (ConvertTo-DotEnvValue $Value)
    for ($index = 0; $index -lt $Lines.Count; $index++) {
        if ($Lines[$index] -match ('^' + [Regex]::Escape($Name) + '=')) {
            $Lines[$index] = $replacement
            return
        }
    }
    $Lines.Add($replacement)
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"
$examplePath = Join-Path $projectRoot ".env.example"

if (Test-Path -LiteralPath $envPath) {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([string[]](Get-Content -LiteralPath $envPath))
}
elseif (Test-Path -LiteralPath $examplePath) {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([string[]](Get-Content -LiteralPath $examplePath))
}
else {
    $lines = [System.Collections.Generic.List[string]]::new()
}

Write-Host "Salesforce read-only setup for Meeting Notes Agent"
Write-Host "Credentials will be stored only in the git-ignored local .env file."
Write-Host "DRY_RUN will remain true, so Salesforce writes stay disabled."
Write-Host ""

$loginUrl = Read-Host "Salesforce login URL [https://login.salesforce.com]"
if ([string]::IsNullOrWhiteSpace($loginUrl)) {
    $loginUrl = "https://login.salesforce.com"
}

$clientId = Read-Host "Connected App consumer key (client ID)"
$clientSecret = ConvertTo-PlainText (Read-Host "Connected App consumer secret" -AsSecureString)
$username = Read-Host "Salesforce username"
$password = ConvertTo-PlainText (Read-Host "Salesforce password" -AsSecureString)
$securityToken = ConvertTo-PlainText (Read-Host "Salesforce security token (leave blank if not required)" -AsSecureString)

if (
    [string]::IsNullOrWhiteSpace($clientId) -or
    [string]::IsNullOrWhiteSpace($clientSecret) -or
    [string]::IsNullOrWhiteSpace($username) -or
    [string]::IsNullOrWhiteSpace($password)
) {
    throw "Client ID, client secret, username, and password are required."
}

Set-DotEnvValue $lines "SALESFORCE_CLIENT_ID" $clientId
Set-DotEnvValue $lines "SALESFORCE_CLIENT_SECRET" $clientSecret
Set-DotEnvValue $lines "SALESFORCE_USERNAME" $username
Set-DotEnvValue $lines "SALESFORCE_PASSWORD" $password
Set-DotEnvValue $lines "SALESFORCE_SECURITY_TOKEN" $securityToken
Set-DotEnvValue $lines "SALESFORCE_LOGIN_URL" $loginUrl.TrimEnd("/")
Set-DotEnvValue $lines "SALESFORCE_API_VERSION" "v60.0"
Set-DotEnvValue $lines "DRY_RUN" "true"

[IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "Saved Salesforce settings to $envPath"
Write-Host "Validating authentication and read access..."
Push-Location $projectRoot
try {
    uv run python -m app.main salesforce-auth-check
}
finally {
    Pop-Location
}
