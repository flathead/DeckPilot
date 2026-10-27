#requires -Version 5.1
# DeckPilot 0.2.0: Windows 10/11, built-in OpenSSH, no Python dependency.
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Launch Setup-Windows.cmd and approve the administrator prompt.'
}
$Base = Join-Path $env:ProgramData 'DeckPilot'
$SshDir = Join-Path $env:ProgramData 'ssh'
$SshBin = Join-Path $env:WINDIR 'System32\OpenSSH'
$Account = 'deckpower'
$PairRule = 'DeckPilot-Pair-Temporary'
$Listener = $null
$Utf8 = New-Object System.Text.UTF8Encoding($false)
function Write-Utf8($Path, $Value) { [IO.File]::WriteAllText($Path, $Value, $Utf8) }
function Native-Check($Description) { if ($LASTEXITCODE -ne 0) { throw $Description } }
Write-Host 'DeckPilot 0.2.0: подключение Windows-ПК'
Write-Host 'Мастер установит OpenSSH и создаст учётную запись для проверки связи и выключения ПК.'
Write-Host 'Со Steam Headless вы играете на Deck через отдельную сессию Steam в Linux-контейнере.'
Write-Host 'Помощник Windows управляет питанием ПК. Для интеграции Headless и Ambxst нужен Linux-ПК.'
if ((Read-Host 'Продолжить установку? [Y/n]') -match '^[nNнН]') { exit }
$Existing = Get-LocalUser -Name $Account -ErrorAction SilentlyContinue
if ($Existing -and $Existing.Description -ne 'DeckPilot restricted SSH account') {
    throw 'An unrelated deckpower account already exists. No account changes were made.'
}
$Capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ($Capability.State -ne 'Installed') {
    $Installed = Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
    if ($Installed.RestartNeeded) { throw 'Windows requests a restart. Restart and run this setup again.' }
    Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue |
        Set-NetFirewallRule -RemoteAddress LocalSubnet -Profile Private
}
if (-not (Test-Path (Join-Path $SshBin 'ssh-keygen.exe'))) {
    Add-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0' | Out-Null
}
if ((Test-Path $Base) -and ((Get-Item -Force $Base).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'The installation directory must not be a link or junction.'
}
New-Item -ItemType Directory -Path $Base -Force | Out-Null
# Root-owned installation; only this fixed account can read the dispatcher and keys.
& icacls.exe $Base /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
Native-Check 'Cannot secure installation directory'
if (-not $Existing) {
    $Bytes = New-Object byte[] 48
    $Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Rng.GetBytes($Bytes) } finally { $Rng.Dispose() }
    $Password = ConvertTo-SecureString ([Convert]::ToBase64String($Bytes)+'aA1!') -AsPlainText -Force
    New-LocalUser -Name $Account -Password $Password -PasswordNeverExpires -UserMayNotChangePassword -Description 'DeckPilot restricted SSH account' | Out-Null
    $Password = $null; [Array]::Clear($Bytes,0,$Bytes.Length)
}
$Sid = (Get-LocalUser -Name $Account).SID.Value
& icacls.exe $Base /grant:r "*${Sid}:(OI)(CI)RX" | Out-Null
Native-Check 'Cannot grant read access to the restricted account'
# Grant only local shutdown; forbid interactive desktop/RDP logons. No admin membership.
Add-Type @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class DeckPilotRights {
 [StructLayout(LayoutKind.Sequential)] struct Obj { public uint Length; public IntPtr Root,Name; public uint Attributes; public IntPtr Descriptor,Qos; }
 [StructLayout(LayoutKind.Sequential)] struct Str { public ushort Length,MaximumLength; public IntPtr Buffer; }
 [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system, ref Obj obj, uint access, out IntPtr policy);
 [DllImport("advapi32.dll")] static extern uint LsaAddAccountRights(IntPtr policy, byte[] sid, Str[] rights, uint count);
 [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr policy);
 [DllImport("advapi32.dll")] static extern uint LsaNtStatusToWinError(uint status);
 static void Check(uint status) { if(status!=0) throw new Win32Exception((int)LsaNtStatusToWinError(status)); }
 public static void Grant(string sidText, string right) {
   var sid=new SecurityIdentifier(sidText); var bytes=new byte[sid.BinaryLength]; sid.GetBinaryForm(bytes,0);
   var obj=new Obj(); obj.Length=(uint)Marshal.SizeOf(typeof(Obj)); IntPtr policy;
   Check(LsaOpenPolicy(IntPtr.Zero, ref obj, 0x810, out policy));
   var str=new Str(); str.Buffer=Marshal.StringToHGlobalUni(right); str.Length=(ushort)(right.Length*2); str.MaximumLength=(ushort)(str.Length+2);
   try { Check(LsaAddAccountRights(policy,bytes,new Str[]{str},1)); }
   finally { Marshal.FreeHGlobal(str.Buffer); LsaClose(policy); }
 }
}
'@
[DeckPilotRights]::Grant($Sid,'SeShutdownPrivilege')
[DeckPilotRights]::Grant($Sid,'SeDenyInteractiveLogonRight')
[DeckPilotRights]::Grant($Sid,'SeDenyRemoteInteractiveLogonRight')
$Dispatcher = @'
$ErrorActionPreference = 'Stop'
switch -CaseSensitive ($env:SSH_ORIGINAL_COMMAND) {
 'check' { Write-Output 'decky-power:ok'; exit 0 }
 'capabilities' { Write-Output '{"protocol":1,"platform":"windows","headless":"","steam_pair":false}'; exit 0 }
 'poweroff' {
   # No /f: applications can prevent shutdown to preserve unsaved data.
   & "$env:WINDIR\System32\shutdown.exe" /s /t 0
   exit $LASTEXITCODE
 }
 default { Write-Error 'Command not allowed'; exit 64 }
}
'@
$CommandPath = Join-Path $Base 'command.ps1'
Write-Utf8 $CommandPath $Dispatcher
$AuthPath = Join-Path $Base 'authorized_keys'
if (-not (Test-Path $AuthPath)) { Write-Utf8 $AuthPath '' }
Start-Service sshd
Set-Service sshd -StartupType Automatic
$ConfigPath = Join-Path $SshDir 'sshd_config'
$Original = [IO.File]::ReadAllText($ConfigPath)
Copy-Item $ConfigPath ($ConfigPath + '.deckpilot-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.bak')
# Remove only our own block; put it before any existing Match so first-match rules apply.
$Clean = [regex]::Replace($Original, '(?ms)^# BEGIN DECKPILOT\r?\n.*?^# END DECKPILOT\r?\n?', '')
$Match = [regex]::Match($Clean,'(?m)^\s*Match\s+')
$Position = if ($Match.Success) { $Match.Index } else { $Clean.Length }
$KeysUnix = $AuthPath.Replace('\','/')
$CommandUnix = $CommandPath.Replace('\','/')
$Block = @"
# BEGIN DECKPILOT
Match User deckpower
    AuthorizedKeysFile "$KeysUnix"
    AuthenticationMethods publickey
    PasswordAuthentication no
    AllowTcpForwarding no
    DisableForwarding yes
    PermitTTY no
    ForceCommand powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$CommandUnix"
Match all
# END DECKPILOT
"@
$Updated = $Clean.Insert($Position,"`r`n"+$Block+"`r`n")
Write-Utf8 $ConfigPath $Updated
& (Join-Path $SshBin 'sshd.exe') -t
if ($LASTEXITCODE -ne 0) { Write-Utf8 $ConfigPath $Original; throw 'SSH config rejected; original restored.' }
$Effective = & (Join-Path $SshBin 'sshd.exe') -T -C 'user=deckpower,host=localhost,addr=127.0.0.1'
Native-Check 'Cannot verify effective SSH config'
if (($Effective -notcontains 'passwordauthentication no') -or ($Effective -notcontains 'authenticationmethods publickey') -or -not ($Effective -match '^forcecommand powershell.exe')) {
    Write-Utf8 $ConfigPath $Original
    throw 'SSH restrictions conflict with this PC configuration. Original config restored.'
}
Restart-Service sshd
$PortLine = $Effective | Where-Object { $_ -match '^port ' } | Select-Object -First 1
$Port = [int](($PortLine -split ' ')[1])
if (-not (Get-NetFirewallRule -Name 'DeckPilot-SSH' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'DeckPilot-SSH' -DisplayName 'DeckPilot SSH (private LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Private | Out-Null
}
Write-Host 'Выберите Ethernet для Wake-on-LAN. Потребуется поддержка сетевой карты и BIOS:'
$Adapters = @(Get-NetAdapter -Physical | Where-Object { $_.MacAddress -and $_.MediaType -eq '802.3' })
for ($i=0;$i -lt $Adapters.Count;$i++) { Write-Host "$($i+1). $($Adapters[$i].Name) $($Adapters[$i].MacAddress)" }
$Mac = ''
if ($Adapters.Count -gt 0) {
    $Choice = Read-Host 'Номер адаптера [1], 0: пропустить'
    if (-not $Choice) { $Choice='1' }
    $Number=0
    if (-not [int]::TryParse($Choice,[ref]$Number) -or $Number -lt 0 -or $Number -gt $Adapters.Count) { throw 'Invalid adapter number' }
    if ($Number -gt 0) {
        $Adapter=$Adapters[$Number-1]; $Mac=$Adapter.MacAddress.Replace('-',':').ToLowerInvariant()
        try { Set-NetAdapterPowerManagement -Name $Adapter.Name -WakeOnMagicPacket Enabled -ErrorAction Stop | Out-Null }
        catch { Write-Warning 'Не удалось включить WoL. Проверьте свойства адаптера в диспетчере устройств и настройки BIOS.' }
    }
}
Write-Host 'Если ПК не просыпается после выключения, проверьте быстрый запуск Windows и питание Ethernet.'
function Fingerprint($Key) {
    $Temp = Join-Path $Base 'fingerprint.tmp'
    try {
        Write-Utf8 $Temp ($Key+"`n")
        $Output = & (Join-Path $SshBin 'ssh-keygen.exe') -lf $Temp
        Native-Check 'Invalid SSH public key'
        $Fp = ($Output -split '\s+')[1]
        if ($Fp -notmatch '^SHA256:[A-Za-z0-9+/]{20,90}$') { throw 'Invalid SSH fingerprint' }
        return $Fp
    } finally { Remove-Item $Temp -Force -ErrorAction SilentlyContinue }
}
$HostFp = Fingerprint ([IO.File]::ReadAllText((Join-Path $SshDir 'ssh_host_ed25519_key.pub')).Trim())
$Code = '{0:D8}' -f (Get-Random -Minimum 0 -Maximum 100000000)
$Deadline = [DateTime]::UtcNow.AddMinutes(5)
$Attempts = 0
$Info = @{protocol=1;name=$env:COMPUTERNAME;mac=$Mac;ssh_port=$Port;fingerprint=$HostFp}
function Reply($Stream,$Status,$Data) {
    $Body=$Utf8.GetBytes(($Data | ConvertTo-Json -Compress))
    $Header=[Text.Encoding]::ASCII.GetBytes("HTTP/1.1 $Status OK`r`nContent-Type: application/json`r`nCache-Control: no-store`r`nConnection: close`r`nContent-Length: $($Body.Length)`r`n`r`n")
    $Stream.Write($Header,0,$Header.Length);$Stream.Write($Body,0,$Body.Length);$Stream.Flush()
}
function Read-Approval {
    Write-Host 'Если код сверки совпадает с кодом на Deck, введите YES и нажмите Enter. На ответ есть 60 секунд:'
    $Until=[DateTime]::UtcNow.AddSeconds(60);$Value=''
    while ([DateTime]::UtcNow -lt $Until -and [DateTime]::UtcNow -lt $Deadline) {
        if ([Console]::KeyAvailable) {
            $Key=[Console]::ReadKey($true)
            if ($Key.Key -eq 'Enter') { Write-Host ''; return $Value -ceq 'YES' }
            if ($Key.Key -eq 'Backspace') { if($Value.Length){$Value=$Value.Substring(0,$Value.Length-1)} }
            elseif ($Value.Length -lt 3) { $Value += $Key.KeyChar; Write-Host -NoNewline $Key.KeyChar }
        }
        Start-Sleep -Milliseconds 100
    }
    return $false
}
try {
    Get-NetFirewallRule -Name $PairRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -Name $PairRule -DisplayName 'DeckPilot pairing (temporary)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47833 -RemoteAddress LocalSubnet -Profile Private | Out-Null
    $Listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Any,47833)
    $Listener.Start()
    Write-Host "`nPIN IN DECKPILOT: $Code"
    Write-Host 'На Deck откройте Decky > DeckPilot > Подключить ПК. Оставьте это окно открытым. PIN действует 5 минут.'
    Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^(127|169\.254)\.' } | Format-Table InterfaceAlias,IPAddress
    $Done=$false
    while (-not $Done -and [DateTime]::UtcNow -lt $Deadline) {
        if (-not $Listener.Pending()) { Start-Sleep -Milliseconds 100; continue }
        $Client=$Listener.AcceptTcpClient();$Client.ReceiveTimeout=5000;$Client.SendTimeout=5000
        try {
            $Stream=$Client.GetStream()
            # Read HTTP headers byte by byte to avoid StreamReader buffering the JSON body.
            $Header=''
            while (-not $Header.EndsWith("`r`n`r`n")) {
                $Byte=$Stream.ReadByte();if($Byte -lt 0){throw 'Disconnected'}
                $Header += [char]$Byte;if($Header.Length -gt 8192){throw 'Oversized header'}
            }
            $Request=($Header -split "`r`n")[0]
            if ($Request -eq 'GET /info HTTP/1.1') { Reply $Stream 200 $Info;continue }
            if ($Request -ne 'POST /pair HTTP/1.1') { Reply $Stream 404 @{error='not found'};continue }
            $Attempts++;if($Attempts -gt 8){Reply $Stream 429 @{error='too many attempts'};continue}
            $LengthMatch=[regex]::Match($Header,'(?im)^Content-Length:\s*([0-9]+)\s*$')
            if(-not $LengthMatch.Success){throw 'No content length'}
            $Length=[int]$LengthMatch.Groups[1].Value
            if($Length -lt 1 -or $Length -gt 1536){throw 'Invalid body size'}
            $Body=New-Object byte[] $Length;$Read=0
            while($Read -lt $Length){$Count=$Stream.Read($Body,$Read,$Length-$Read);if($Count -le 0){throw 'Truncated body'};$Read+=$Count}
            $Payload=$Utf8.GetString($Body)|ConvertFrom-Json
            if($Payload.code -isnot [string] -or $Payload.code -cne $Code){Reply $Stream 403 @{error='incorrect code'};continue}
            if($Payload.public_key -isnot [string] -or $Payload.public_key -notmatch '^ssh-ed25519 ([A-Za-z0-9+/]{40,256}={0,2})$'){Reply $Stream 400 @{error='invalid key'};continue}
            $KeyText=$Payload.public_key
            $DeckFp=Fingerprint $KeyText
            $Hash=[Security.Cryptography.SHA256]::Create()
            try {$Digest=([BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::ASCII.GetBytes($HostFp+'|'+$DeckFp)))).Replace('-','').Substring(0,12)} finally {$Hash.Dispose()}
            $Verification=$Digest.Substring(0,4)+'-'+$Digest.Substring(4,4)+'-'+$Digest.Substring(8,4)
            Write-Host "`nКОД СВЕРКИ: $Verification"
            if(-not (Read-Approval)){Reply $Stream 403 @{error='not approved'};continue}
            $ExistingKeys=[IO.File]::ReadAllText($AuthPath)
            # ForceCommand is enforced for the entire dedicated account in sshd_config.
            if(-not $ExistingKeys.Contains($KeyText)){Write-Utf8 $AuthPath ($ExistingKeys.TrimEnd()+"`n"+$KeyText+"`n")}
            Reply $Stream 200 @{success=$true;fingerprint=$HostFp;mac=$Mac;name=$env:COMPUTERNAME}
            $Done=$true
        } catch {
            Write-Warning 'Запрос отклонён или связь прервалась. Повторите подключение в DeckPilot.'
            try { Reply $Stream 400 @{error='invalid request'} } catch {}
        } finally { $Client.Close() }
    }
    if($Done){Write-Host 'Сопряжение выполнено. DeckPilot проверит вход по SSH.'}else{Write-Host 'Время подключения истекло. Запустите мастер ещё раз.'}
} finally {
    if($Listener){$Listener.Stop()}
    Get-NetFirewallRule -Name $PairRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}
