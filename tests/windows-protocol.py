#!/usr/bin/env python3
"""Exercise Windows pairing HTTP under PowerShell on Linux, without host setup.
Usage: DECKPILOT_PWSH=/path/to/pwsh python3 tests/windows-protocol.py
Windows service/account/ACL/shutdown still require a real Windows machine.
"""
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
pwsh=os.environ['DECKPILOT_PWSH']
source=(ROOT/'tools/setup-windows.ps1').read_text()
with tempfile.TemporaryDirectory(prefix='deckpilot-windows-test-') as temp:
    base=Path(temp)
    subprocess.run(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-f',str(base/'ssh_host_ed25519_key')],check=True)
    (base/'ssh-keygen.exe').symlink_to('/usr/bin/ssh-keygen')
    (base/'authorized_keys').write_text('')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    tail=source.split('function Fingerprint($Key)',1)[1]
    tail='function Fingerprint($Key)'+tail
    start=tail.index('function Read-Approval {')
    stop=tail.index('\ntry {\n    Get-NetFirewallRule',start)
    tail=tail[:start]+'''function Read-Approval { return $true }
'''+tail[stop:]
    tail=tail.replace('([Net.IPAddress]::Any,47833)',f'([Net.IPAddress]::Loopback,{port})')
    prelude=f'''$ErrorActionPreference='Stop'
$Base='{base}';$SshDir=$Base;$SshBin=$Base;$AuthPath=Join-Path $Base 'authorized_keys';$Port=2222;$Mac='00:11:22:33:44:55'
$Utf8=New-Object System.Text.UTF8Encoding($false)
function Write-Utf8($Path,$Value){{[IO.File]::WriteAllText($Path,$Value,$Utf8)}}
function Native-Check($Description){{if($LASTEXITCODE -ne 0){{throw $Description}}}}
function Get-NetFirewallRule {{}}
function New-NetFirewallRule {{}}
function Remove-NetFirewallRule {{}}
function Get-NetIPAddress {{}}
'''
    script=base/'server.ps1';script.write_text(prelude+tail)
    process=subprocess.Popen([pwsh,'-NoProfile','-File',str(script)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        while True:
            line=process.stdout.readline()
            if not line:raise RuntimeError('Windows harness failed before starting')
            match=re.search(r'PIN В DECKPILOT: (\d{8})',line)
            if match:code=match.group(1);break
        def request(method,path,payload=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            try:
                conn.request(method,path,None if payload is None else json.dumps(payload))
                result=conn.getresponse();return result.status,json.loads(result.read())
            finally:conn.close()
        status,info=request('GET','/info')
        assert status==200 and info['protocol']==1 and info['ssh_port']==2222, info
        public=(base/'ssh_host_ed25519_key.pub').read_text().split()
        key=' '.join(public[:2])
        assert request('POST','/pair',{'code':'bad','public_key':key})[0]==403
        assert request('POST','/pair',{'code':code,'public_key':'ssh-ed25519 INVALID'})[0]==400
        assert not (base/'authorized_keys').read_text()
        status,response=request('POST','/pair',{'code':code,'public_key':key})
        assert status==200 and response['success'],response
        assert response['fingerprint']==info['fingerprint']
        process.wait(timeout=5)
        assert process.returncode==0
        assert (base/'authorized_keys').read_text().strip()==key
        print('Windows PowerShell HTTP protocol: info, invalid PIN, invalid key, approved pair, key persistence — passed')
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=5)
