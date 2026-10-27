import asyncio
import concurrent.futures
import http.client
import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import shutil
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.modules['decky'] = types.SimpleNamespace(DECKY_PLUGIN_SETTINGS_DIR='/tmp/deckpilot-unused', logger=logging.getLogger('deckpilot-test'))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


backend = module('backend', ROOT/'main.py')
bridge = module('bridge', ROOT/'tools/headless_bridge.py')
pair = module('pair', ROOT/'tools/pair_server.py')


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patch = patch.object(Path, 'home', return_value=self.home)
        self.patch.start()
        self.plugin = backend.Plugin()
        self.plugin.store = self.home/'settings/DeckPilot/hosts.json'
        self.host = {'id':'pc1', 'name':'PC', 'ip':'192.168.1.10', 'mac':'00:11:22:33:44:55',
                     'ssh_port':22, 'platform':'linux', 'headless':'ambxst', 'steam_pair':True, 'show_headless':False}
        self.plugin.hosts = [self.host]
        self.plugin._save()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def call(self, name, *args):
        return asyncio.run(getattr(self.plugin, name)(*args))

    def test_reject_public_ips_and_invalid_macs(self):
        for ip in ('127.0.0.1', '8.8.8.8', '192.168.1.1;poweroff', 'https://192.168.1.1'):
            with self.assertRaises(ValueError): backend.local_ip(ip)
        for mac in ('ff:ff:ff:ff:ff:ff','00:00:00:00:00:00','01:11:22:33:44:55'):
            with self.assertRaises(ValueError): backend.normalize_mac(mac)

    def test_persistence_and_settings_preserve_opt_in(self):
        self.plugin._load()
        self.assertFalse(self.plugin.hosts[0]['show_headless'])
        self.assertEqual(self.plugin.hosts[0]['headless'], 'ambxst')
        result = self.call('save_host','pc1','New name','192.168.1.10','00:11:22:33:44:55',22)
        self.assertTrue(result['success'])
        self.assertTrue(result['host']['steam_pair'])
        self.assertFalse(result['host']['show_headless'])
        self.assertEqual(self.plugin.store.stat().st_mode & 0o777, 0o600)

    def test_enable_later_requires_verified_capability(self):
        with patch.object(backend,'_ssh_request',return_value='decky-power:ok'), patch.object(backend,'_capabilities',return_value={'headless':'','platform':'linux','steam_pair':False}):
            self.assertFalse(self.call('set_headless_visibility','pc1',True)['success'])
        with patch.object(backend,'_ssh_request',return_value='decky-power:ok'), patch.object(backend,'_capabilities',return_value={'headless':'ambxst','platform':'linux','steam_pair':True}):
            self.assertTrue(self.call('set_headless_visibility','pc1',True)['host']['show_headless'])
        self.assertTrue(self.call('set_headless_visibility','pc1',False)['success'])

    def test_actions_are_allowlisted_and_pin_stays_in_stdin(self):
        self.assertFalse(self.call('headless_action','pc1','open-shell')['success'])
        self.assertFalse(self.call('headless_action','pc1','pair','12;3')['success'])
        with patch.object(backend,'_ssh_request',return_value='{"message":"ok"}') as request:
            result = self.call('headless_action','pc1','pair','1234')
            self.assertTrue(result['success'])
            self.assertEqual(request.call_args.args[2:], ('headless-pair','1234\n',20))

    def test_controller_error_is_not_success(self):
        with patch.object(backend,'_ssh_request',return_value='{"error":"busy"}'):
            self.assertEqual(self.call('headless_action','pc1','start')['error'],'busy')

    def test_legacy_import_does_not_overwrite_current(self):
        self.assertFalse(self.call('import_legacy')['success'])
        previous = self.plugin.store.parent.parent/'PC On Off/hosts.json'
        previous.parent.mkdir()
        previous.write_text(json.dumps([self.host]))
        self.plugin.hosts=[];self.plugin._save()
        self.assertEqual(self.call('import_legacy')['count'],1)
        self.assertTrue(previous.exists())

    def test_key_generation_and_permissions(self):
        public, fp = backend._private_pubkey(self.plugin.key)
        self.assertTrue(public.startswith('ssh-ed25519 '))
        self.assertTrue(fp.startswith('SHA256:'))
        self.assertEqual(self.plugin.key.stat().st_mode & 0o777,0o600)
        self.assertEqual(backend._private_pubkey(self.plugin.key),(public,fp))

    def test_pair_rejects_host_changed_since_display(self):
        fp='SHA256:'+'a'*43
        with patch.object(backend,'_private_pubkey',return_value=('key',fp)), patch.object(backend,'_pair_info',return_value={'fingerprint':fp}), patch.object(backend,'_pair_submit') as submit:
            result=self.call('pair_host','192.168.1.10','12345678','SHA256:'+'b'*43)
            self.assertFalse(result['success']);submit.assert_not_called()

    def test_pair_flow_checks_both_hostkeys_and_persists(self):
        fp='SHA256:'+'a'*43
        info={'fingerprint':fp,'ip':'192.168.1.10','name':'PC','mac':'00:11:22:33:44:55','ssh_port':22}
        with patch.object(backend,'_private_pubkey',return_value=('pub',fp)), patch.object(backend,'_pair_info',return_value=info), patch.object(backend,'_scan_host_key',return_value=('192.168.1.10 ssh-ed25519 AAAA',fp)), patch.object(backend,'_pair_submit',return_value={'mac':info['mac']}), patch.object(backend,'_store_known_host'), patch.object(backend,'_try_ssh_check'), patch.object(backend,'_capabilities',return_value={'headless':'ambxst','steam_pair':True,'platform':'linux'}):
            result=self.call('pair_host',info['ip'],'12345678',fp,False)
            self.assertTrue(result['success']);self.assertFalse(result['host']['show_headless'])
            result=self.call('pair_host',info['ip'],'12345678',fp,True)
            self.assertTrue(result['host']['show_headless'])

    def test_changed_key_after_pair_not_saved(self):
        fp='SHA256:'+'a'*43
        info={'fingerprint':fp,'ip':'192.168.1.10','ssh_port':22}
        with patch.object(backend,'_private_pubkey',return_value=('pub',fp)), patch.object(backend,'_pair_info',return_value=info), patch.object(backend,'_scan_host_key',side_effect=[('line',fp),('new','SHA256:'+'b'*43)]), patch.object(backend,'_pair_submit',return_value={}), patch.object(backend,'_store_known_host') as store:
            result=self.call('pair_host',info['ip'],'12345678',fp)
            self.assertFalse(result['success']);store.assert_not_called()

    def test_ssh_always_pins_host_and_does_not_use_shell(self):
        with patch.object(backend.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='{}')) as run:
            backend._ssh_request(self.plugin.key,self.host,'headless-pair','1234\n')
            args=run.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertEqual(args[-1],'headless-pair')
            self.assertNotIn('1234',' '.join(args))
            self.assertNotIn('shell',run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs['input'],'1234\n')


class BridgeTests(unittest.TestCase):
    config={'mode':'ambxst','user':'gamer','uid':1000,'home':'/home/gamer','controller':'/home/gamer/.local/bin/steam-headless'}

    def test_drop_privileges_and_use_controller(self):
        with patch.object(bridge,'run',return_value=types.SimpleNamespace(stdout='{"running":false}',returncode=0)) as run:
            self.assertFalse(bridge.dispatch(self.config,'status')['running'])
            args=run.call_args.args[0]
            self.assertEqual(args[:4],['/usr/bin/runuser','-u','gamer','--'])
            self.assertEqual(args[-2:],[self.config['controller'],'status'])

    def test_generic_docker_targets_only_named_container_and_no_sigkill(self):
        with patch.object(bridge,'run',side_effect=[types.SimpleNamespace(returncode=0),types.SimpleNamespace(returncode=0,stdout='{"Running":false,"Status":"exited"}')]) as run:
            bridge.dispatch({'mode':'docker','container':'steam-headless'},'stop')
            self.assertEqual(run.call_args_list[0].args[0][-3:],['stop','--time=-1','steam-headless'])

    def test_injection_and_unknown_actions_rejected(self):
        with self.assertRaises(ValueError):bridge.dispatch({'mode':'docker','container':'--all'},'stop')
        with self.assertRaises(ValueError):bridge.dispatch(self.config,'shell')
        with self.assertRaises(ValueError):bridge.dispatch(self.config,'pair','1234;id')

    def test_none_and_windows_do_not_expose_headless(self):
        self.assertEqual(bridge.dispatch({},'capabilities')['headless'],'')
        self.assertFalse(bridge.dispatch({'mode':'docker'},'capabilities')['steam_pair'])


class LinuxConfigTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('sshd'), 'OpenSSH server required for parser check')
    def test_match_block_is_after_global_options_and_setup_is_repeatable(self):
        template=(ROOT/'tools/setup-linux.template.sh').read_text()
        code=template.split("<<'SSHCONFIG'\n",1)[1].split('\nSSHCONFIG',1)[0]
        with tempfile.TemporaryDirectory() as temp:
            key=Path(temp)/'host_key'
            subprocess.run(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
            config=Path(temp)/'sshd_config'
            config.write_text(f'HostKey {key}\nUsePAM no\nPort 22222\nMatch User somebody\n    PermitTTY no\n')
            code=code.replace("Path('/etc/ssh/sshd_config')",'Path('+repr(str(config))+')')
            exec(code,{})
            exec(code,{})
            self.assertEqual(config.read_text().count('# BEGIN DECKPILOT'),1)
            parsed=subprocess.run([shutil.which('sshd'),'-T','-f',str(config),'-C','user=deckpower,host=localhost,addr=127.0.0.1'],text=True,capture_output=True)
            self.assertEqual(parsed.returncode,0,parsed.stderr)
            self.assertIn('forcecommand /usr/local/libexec/decky-power-command',parsed.stdout.lower())
            self.assertIn('disableforwarding yes',parsed.stdout.lower())


class PairServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.auth=Path(self.tmp.name)/'.ssh/authorized_keys'
        self.auth_patch=patch.object(pair,'AUTH',self.auth);self.auth_patch.start()
        self.public,self.fp=backend._private_pubkey(Path(self.tmp.name)/'keys/ed25519')
        pair.state=types.SimpleNamespace(code='12345678',deadline=time.monotonic()+30,
            retries=0,lock=threading.Lock(),requests=queue.Queue(maxsize=1),done=threading.Event(),
            mac='00:11:22:33:44:55',ssh_port=22,fp=self.fp,name='test-host',pending=False)
        class Server(pair.ThreadingHTTPServer):daemon_threads=True
        self.server=Server(('127.0.0.1',0),pair.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.auth_patch.stop();self.tmp.cleanup()

    def request(self, code, public=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            conn.request('POST','/pair',json.dumps({'code':code,'public_key':public or self.public}),{'Content-Type':'application/json'})
            result=conn.getresponse()
            return result.status,json.loads(result.read())
        finally:conn.close()

    def test_invalid_code_and_rate_limit_never_write_key(self):
        self.assertEqual(self.request('bad')[0],403)
        pair.state.retries=8
        self.assertEqual(self.request('12345678')[0],429)
        self.assertFalse(self.auth.exists())

    def test_real_http_pair_requires_explicit_approval(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(self.request,'12345678')
            pending=pair.state.requests.get(timeout=3)
            self.assertFalse(self.auth.exists())
            self.assertEqual(self.request('12345678')[0],409)
            pair.write_authorized(pending['key']);pending['ok']=True;pending['approved'].set()
            status,result=future.result(timeout=3)
        self.assertEqual(status,200);self.assertTrue(result['success'])
        content=self.auth.read_text()
        self.assertIn('restrict,command="/usr/local/libexec/decky-power-command"',content)
        self.assertEqual(self.auth.stat().st_mode & 0o777,0o600)
        pair.write_authorized(self.public)
        self.assertEqual(self.auth.read_text().count(self.public),1)


if __name__=='__main__':unittest.main()
