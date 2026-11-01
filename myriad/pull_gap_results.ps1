<#
.SYNOPSIS
    Copy PyPSA-GB gap-dispatch results (solved networks + summaries) from Myriad.
    RUN THIS IN YOUR OWN VPN-connected PowerShell terminal (not via Claude).
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File `
      "C:\Users\ucbqnge\Assume_GB\assume-gb\myriad\pull_gap_results.ps1"
.NOTES
    Uses scp (bundled with Windows OpenSSH). You'll be prompted for your UCL
    password unless you've set up an SSH key to Myriad.
#>
param(
    [string]$User   = 'ucbqnge',
    [string]$Server = 'myriad.rc.ucl.ac.uk',
    [string]$Remote = 'Assume_GB/pypsa-gb/resources/network',
    [string]$Local  = 'C:\Users\ucbqnge\Assume_GB\results\pypsa_gb_gap'
)

$opt = @("-o","ConnectTimeout=30","-o","ServerAliveInterval=15")
New-Item -ItemType Directory -Force -Path $Local | Out-Null

Write-Host ">> Pulling *_solved.nc and *_optimization_summary.txt"
Write-Host ">> $User@$Server:$Remote -> $Local"

# Solved networks (the main results) and the per-scenario optimisation summaries.
scp @opt "${User}@${Server}:${Remote}/*_solved.nc" $Local
scp @opt "${User}@${Server}:${Remote}/*_optimization_summary.txt" $Local

Write-Host ">> Done. Local files:"
Get-ChildItem $Local | Select-Object Name, Length | Format-Table -AutoSize
