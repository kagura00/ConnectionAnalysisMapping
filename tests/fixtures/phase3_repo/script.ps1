Import-Module ./module.psm1

function Get-Thing {
  param([string]$Name)
  Write-Output $Name
}

Get-Thing -Name $env:NAME
