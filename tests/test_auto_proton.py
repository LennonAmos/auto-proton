import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_proton as a
import lifecycle

CONFIG = '''// keep this comment and unrelated spelling/whitespace
"InstallConfigStore" { "Software" { "Valve" { "Steam" {
"Unrelated" "value\\\\path"
"CompatToolMapping" { "0" {"name" "global" "priority" "75"}
"123" { "name" "proton_9" "config" "" "priority" "250" } }
} } } }
'''
AFTER = {'name': 'proton_10', 'config': '', 'priority': '250'}
TOOLS = {k: {'name': k, 'display': k} for k in ('proton_9', 'proton_10', 'proton_experimental', 'GE-Proton11-11-x86_64')}
NOW = 1793369000


def report(who='a', version='10.0-3', **extra):
    response = dict(variant='official', protonVersion=version, verdict='yes', stabilityFaults='no', significantBugs='no', opens='yes', startsPlay='yes')
    response.update(extra)
    return dict(timestamp=NOW-100, responses=response, contributor={'id': who}, device={'hardwareType': 'pc', 'inferred': {'steam': {'gpu': 'NVIDIA GeForce'}}})


class Parsing(unittest.TestCase):
    def test_surgical_edit(self):
        old = a.mapping_node(CONFIG)[1]
        entry = a.child(old.value, '123')
        changed = a.edit_mapping(CONFIG, {'123': AFTER})
        self.assertEqual(changed, CONFIG[:entry.start] + a.render_entry('123', AFTER) + CONFIG[entry.end:])
        self.assertEqual(a.mappings(changed)['0']['name'], 'global')
        self.assertEqual(a.mappings(changed)['123'], AFTER)

    def test_native_clears_only_app(self):
        changed = a.edit_mapping(CONFIG, {'123': None})
        self.assertNotIn('123', a.mappings(changed))
        self.assertEqual(a.mappings(changed)['0']['name'], 'global')

    def test_create_mapping(self):
        empty = '"InstallConfigStore" {"Software" {"Valve" {"Steam" {"keep" "yes"}}}}'
        self.assertEqual(a.mappings(a.edit_mapping(empty, {'123': AFTER}))['123'], AFTER)

    def test_reject_malformed(self):
        for text in ['"x" {', '"x" "unterminated', '"x" "y" }', '"x" "a" "X" "b"']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                a.parse(text)

    def test_tool_case_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root/'compatibilitytools.d/GE'
            folder.mkdir(parents=True)
            (folder/'proton').touch()
            (folder/'compatibilitytool.vdf').write_text('"compatibilitytools" {"compat_tools" {"GE-Proton11-11-x86_64" {"from_oslist" "windows" "to_oslist" "linux"}}}')
            self.assertIn('GE-Proton11-11-x86_64', a.installed_tools(root, {}, {}))

    def test_appinfo_versions(self):
        for version in (39, 40, 41):
            def key(name):
                return struct.pack('<I', ['appinfo','common','oslist'].index(name)) if version == 41 else name.encode()+b'\0'
            payload = b'\x00'+key('appinfo')+b'\x00'+key('common')+b'\x01'+key('oslist')+b'linux\0\x08\x08\x08'
            header = struct.pack('<II', 0x07564400+version, 1)
            record = bytes(40+(20 if version>=40 else 0))+payload
            entries = struct.pack('<II', 123, len(record))+record+bytes(4)
            if version == 41:
                header += struct.pack('<Q', 16+len(entries))
                entries += struct.pack('<I', 3)+b'appinfo\0common\0oslist\0'
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'appinfo.vdf';path.write_bytes(header+entries)
                self.assertEqual(a.appinfo(path)['123']['common']['oslist'], 'linux')

    def test_unknown_appinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'data';p.write_bytes(bytes(20))
            with self.assertRaises(ValueError): a.appinfo(p)


class Ranking(unittest.TestCase):
    def rank(self, rows, vendor='nvidia'):
        return a.recommend(rows, TOOLS, vendor, NOW)[0]

    def test_requires_independent_consensus(self):
        self.assertIsNone(self.rank([report(), report()]))
        self.assertEqual(self.rank([report('a'), report('b')]), 'proton_10')

    def test_failure_veto(self):
        self.assertIsNone(self.rank([report('a'), report('b'), report('c', verdict='no')]))

    def test_hardware_and_age(self):
        rows=[report('a'),report('b')]
        self.assertIsNone(self.rank(rows,'amd'))
        rows[0]['timestamp']=NOW-181*a.DAY
        self.assertIsNone(self.rank(rows))

    def test_tweaks_not_proof(self):
        for extra in ({'launchOptions':'rm -rf anything'}, {'verdictOob':'no'}, {'stabilityFaults':'yes'}, {'tinkerOverride':'yes'}, {'significantBugs':'yes'}):
            self.assertIsNone(self.rank([report('a'),report('b',**extra)]))

    def test_variant_not_misread_as_official(self):
        self.assertIsNone(self.rank([report('a',variant='ge',customProtonVersion='GE-Proton11-7'),report('b',variant='ge',customProtonVersion='GE-Proton11-7')]))
        self.assertIsNone(self.rank([report('a',variant='notListed'),report('b',variant='notListed')]))

    def test_ge_exact_version(self):
        self.assertEqual(self.rank([report('a',variant='ge',customProtonVersion='GE-Proton11-11'),report('b',variant='ge',customProtonVersion='GE-Proton11-11')]), 'GE-Proton11-11-x86_64')

    def test_newer_stable_tiebreak(self):
        self.assertEqual(self.rank([report('a'),report('b'),report('c','9.0-4'),report('d','9.0-4')]),'proton_10')

    def test_uninstalled_proton_not_selected(self):
        self.assertIsNone(self.rank([report('a','11.0-2'),report('b','11.0-2')]))


class Transactions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'Steam';self.state=Path(self.tmp.name)/'state'
        self.config=self.root/'config/config.vdf';self.config.parent.mkdir(parents=True);self.config.write_text(CONFIG)
        self.plan={'games':[{'appid':'123','action':'set','before':a.mappings(CONFIG)['123'],'after':AFTER}]}

    def test_running_steam_never_written(self):
        with patch.object(a,'steam_running',return_value=True):
            self.assertIn('deferred',a.apply(self.root,self.plan,self.state))
        self.assertEqual(self.config.read_text(),CONFIG)
        self.assertFalse(self.state.exists())

    def test_changed_settings_cancel_write(self):
        self.config.write_text(a.edit_mapping(CONFIG,{'123':None}))
        with patch.object(a,'steam_running',return_value=False),self.assertRaises(ValueError):
            a.apply(self.root,self.plan,self.state)

    def test_steam_starting_cancels_write(self):
        with patch.object(a,'steam_running',side_effect=[False,True]),self.assertRaises(ValueError):
            a.apply(self.root,self.plan,self.state)
        self.assertEqual(self.config.read_text(),CONFIG)

    def test_transaction_and_backup(self):
        with patch.object(a,'steam_running',return_value=False):
            a.apply(self.root,self.plan,self.state)
        self.assertEqual(a.mappings(self.config.read_text())['123'],AFTER)
        self.assertEqual((self.state/'config-before-last-apply.vdf').read_text(),CONFIG)
        self.assertEqual(json.loads((self.state/'owned.json').read_text())['123']['before'],self.plan['games'][0]['before'])
        self.assertFalse((self.state/'transaction.json').exists())

    def test_recover_interrupted_write(self):
        import hashlib
        after=a.edit_mapping(CONFIG,{'123':AFTER})
        self.config.write_text(after)
        owned={'123':{'before':None,'after':AFTER}}
        a.save_json(self.state/'transaction.json',{'sha256_before':hashlib.sha256(CONFIG.encode()).hexdigest(),'sha256_after':hashlib.sha256(after.encode()).hexdigest(),'owned':owned})
        a.recover(self.root,self.state)
        self.assertEqual(json.loads((self.state/'owned.json').read_text()),owned)

    def test_native_and_manual_detection(self):
        games={'123':{'name':'Native','stateflags':'4'}}
        info={'123':{'common':{'oslist':'linux,windows','type':'Game'}}}
        with patch.object(a,'inventory',return_value=(games,[])),patch.object(a,'appinfo',return_value=info),patch.object(a,'installed_tools',return_value=TOOLS):
            result=a.plan(self.root,None,self.state)
            self.assertIsNone(result['games'][0]['after'])
            self.assertEqual(result['games'][0]['action'],'set')
            a.save_json(self.state/'owned.json',{'123':{'before':a.mappings(CONFIG)['123'],'after':None}})
            result=a.plan(self.root,None,self.state)
            self.assertEqual(result['games'][0]['action'],'set')
            self.assertIsNone(result['games'][0]['after'])
            info['123']['common']['oslist']='windows'
            a.save_json(self.state/'owned.json',{'123':{'before':None,'after':AFTER}})
            result=a.plan(self.root,unittest.mock.Mock(reports=unittest.mock.Mock(return_value=[])),self.state)
            self.assertIn('Manual change',result['games'][0]['reason'])

    def test_unmounted_library_does_not_delete_mapping(self):
        a.save_json(self.state/'owned.json',{'123':{'before':None,'after':a.mappings(CONFIG)['123']}})
        with patch.object(a,'inventory',return_value=({},['/unmounted'])),patch.object(a,'appinfo',return_value={}),patch.object(a,'installed_tools',return_value={}):
            self.assertEqual(a.plan(self.root,None,self.state)['games'],[])

    def test_deleted_game_restores_original(self):
        a.save_json(self.state/'owned.json',{'123':{'before':None,'after':a.mappings(CONFIG)['123']}})
        with patch.object(a,'inventory',return_value=({},[])),patch.object(a,'appinfo',return_value={}),patch.object(a,'installed_tools',return_value={}):
            row=a.plan(self.root,None,self.state)['games'][0]
            self.assertEqual(row['action'],'set');self.assertIsNone(row['after'])


class Lifecycle(unittest.TestCase):
    def test_install_remove_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            p={k:home/k/'auto-proton' for k in ('app','cache','state')}
            p.update(units=home/'units',bin=home/'bin')
            steam=home/'config/config.vdf';steam.parent.mkdir();steam.write_text(CONFIG)
            def control(*args,**kwargs):
                self.assertNotIn('steam', ' '.join(args).lower())
                return subprocess.CompletedProcess([],0)
            with patch.object(lifecycle,'locations',return_value=p),patch.object(lifecycle,'control',side_effect=control),patch.object(lifecycle.shutil,'which',return_value='/usr/bin/systemctl'):
                lifecycle.install(home)
                self.assertTrue((p['app']/'install.json').exists())
                lifecycle.install(home)
                for key in ('cache','state'):
                    p[key].mkdir(parents=True);(p[key]/'test').touch()
                lifecycle.uninstall()
                lifecycle.uninstall()
            self.assertFalse(any(p[k].exists() for k in ('app','cache','state')))
            self.assertEqual(list(p['bin'].iterdir()),[])
            self.assertEqual(list(p['units'].iterdir()),[])
            self.assertEqual(steam.read_text(),CONFIG)

    def test_install_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);(home/'config').mkdir();(home/'config/config.vdf').write_text(CONFIG);p={k:home/k/'auto-proton' for k in ('app','cache','state')};p.update(units=home/'units',bin=home/'bin')
            def control(*args,**kwargs):
                if 'enable' in args:raise subprocess.CalledProcessError(1,[],stderr='test failure')
            with patch.object(lifecycle,'locations',return_value=p),patch.object(lifecycle,'control',side_effect=control):
                with self.assertRaises(subprocess.CalledProcessError):lifecycle.install(home)
            self.assertFalse(p['app'].exists());self.assertEqual(list(p['bin'].iterdir()),[]);self.assertEqual(list(p['units'].iterdir()),[])



class LiveIntegration(unittest.TestCase):
    def test_default_runtime_is_not_forced_override(self):
        self.assertEqual(a.canonical_live({'123':{'name':'steamlinuxruntime','priority':75}}),{})
        self.assertEqual(a.canonical_live({'123':{'name':'proton_10','priority':250}}),{'123':AFTER})

    def test_live_confirmed_and_ownership_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);ws=unittest.mock.Mock()
            before=a.mappings(CONFIG)['123']
            result={'games':[{'appid':'123','action':'set','before':before,'after':AFTER}]}
            def shot(values):return {'123':{'name':values['name'],'priority':250}}
            with patch.object(a.live_steam,'connect',return_value=ws),patch.object(a.live_steam,'snapshot',side_effect=[shot(before),shot(AFTER)]),patch.object(a.live_steam,'set_tool') as setter:
                self.assertIn('verified 1',a.apply_live(Path(tmp),result,state))
                setter.assert_called_once_with(ws,'123','proton_10')
            self.assertEqual(json.loads((state/'owned.json').read_text())['123']['after'],AFTER)
            self.assertFalse((state/'live-transaction.json').exists());ws.close.assert_called_once()

    def test_live_rejected_setting_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);ws=unittest.mock.Mock();before=a.mappings(CONFIG)['123']
            result={'games':[{'appid':'123','action':'set','before':before,'after':AFTER}]}
            with patch.object(a.live_steam,'connect',return_value=ws),patch.object(a.live_steam,'snapshot',side_effect=[{'123':{'name':'proton_9','priority':250}},{'123':{'name':'proton_9','priority':250}}]),patch.object(a.live_steam,'set_tool',side_effect=[ValueError('rejected'),True]) as setter:
                with self.assertRaises(ValueError):a.apply_live(Path(tmp),result,state)
                self.assertEqual(setter.call_args_list[-1].args,(ws,'123','proton_9'))
            self.assertFalse((state/'owned.json').exists());self.assertFalse((state/'live-transaction.json').exists())

    def test_live_manual_change_cancels(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws=unittest.mock.Mock()
            result={'games':[{'appid':'123','action':'set','before':None,'after':AFTER}]}
            with patch.object(a.live_steam,'connect',return_value=ws),patch.object(a.live_steam,'snapshot',return_value={'123':{'name':'proton_9','priority':250}}),patch.object(a.live_steam,'set_tool') as setter:
                with self.assertRaises(ValueError):a.apply_live(Path(tmp),result,Path(tmp))
                setter.assert_not_called()

    def test_live_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);owned={'123':{'before':None,'after':AFTER}}
            a.save_json(state/'live-transaction.json',{'appid':'123','before':None,'after':AFTER,'owned':owned})
            with patch.object(a.live_steam,'snapshot',return_value={'123':{'name':'proton_10','priority':250}}):
                a.recover_live(Path(tmp),state,unittest.mock.Mock())
            self.assertEqual(json.loads((state/'owned.json').read_text()),owned)

    def test_remote_debugger_endpoint_refused(self):
        from live_steam import WebSocket
        for url in ['ws://evil.example:8080/devtools/page/ABC','ws://127.0.0.1:80/devtools/page/ABC','ws://127.0.0.1:8080/other']:
            with self.subTest(url=url),self.assertRaises(ValueError):WebSocket(url,8080)

    def test_websocket_fragment_and_ping(self):
        import socket
        from live_steam import WebSocket
        client,server=socket.socketpair()
        try:
            w=WebSocket.__new__(WebSocket);w.sock=client;w.buffer=bytearray()
            server.sendall(b'\x01\x05{"x":'+b'\x89\x01p'+b'\x80\x02' + b'1}')
            self.assertEqual(w.receive(),{'x':1})
            self.assertEqual(server.recv(20)[0],0x8a)
        finally:client.close();server.close()

    def test_websocket_masks_client_messages(self):
        import socket
        from live_steam import WebSocket
        client,server=socket.socketpair()
        try:
            w=WebSocket.__new__(WebSocket);w.sock=client;w.buffer=bytearray()
            w.send(b'hello')
            packet=server.recv(100);self.assertEqual(packet[1],0x85)
            mask=packet[2:6]
            self.assertEqual(bytes(c ^ mask[i%4] for i,c in enumerate(packet[6:])),b'hello')
        finally:client.close();server.close()

if __name__ == '__main__': unittest.main()

class ManualControls(unittest.TestCase):
    def test_preferences_are_persistent_and_validated(self):
        import preferences
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            preferences.set_manual(state,'123',True)
            self.assertEqual(preferences.read(state),{'123':True})
            preferences.set_manual(state,'123',False)
            self.assertEqual(preferences.read(state),{'123':False})
            for appid,enabled in [('0',True),('bad',True),('123',1)]:
                with self.assertRaises(ValueError):preferences.set_manual(state,appid,enabled)
            (state/'overrides.json').write_text('{"123": "yes"}')
            with self.assertRaises(ValueError):preferences.read(state)

    def test_manual_choice_retains_recommendation_and_auto_resumes(self):
        import preferences
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state';root=Path(tmp)/'Steam';config=root/'config/config.vdf'
            config.parent.mkdir(parents=True);config.write_text(CONFIG)
            games={'123':{'name':'Game','stateflags':'4'}}
            info={'123':{'common':{'oslist':'windows','type':'Game'}}}
            db=unittest.mock.Mock();db.reports.return_value=[report('a'),report('b')]
            a.save_json(state/'owned.json',{'123':{'before':None,'after':AFTER}})
            with patch.object(a,'inventory',return_value=(games,[])),patch.object(a,'appinfo',return_value=info),patch.object(a,'installed_tools',return_value=TOOLS),patch.object(a,'steam_running',return_value=False),patch.object(a,'gpu_vendor',return_value='nvidia'),patch.object(a.time,'time',return_value=NOW):
                preferences.set_manual(state,'123',True)
                row=a.plan(root,db,state)['games'][0]
                self.assertEqual(row['action'],'skip');self.assertEqual(row['recommended'],AFTER)
                preferences.set_manual(state,'123',False)
                row=a.plan(root,db,state)['games'][0]
                self.assertEqual(row['action'],'set');self.assertEqual(row['after'],AFTER)
                info['123']['common']['oslist']='linux,windows'
                preferences.set_manual(state,'123',True)
                self.assertEqual(a.plan(root,db,state)['games'][0]['action'],'skip')

    def test_override_enabled_after_scan_blocks_write(self):
        import preferences
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state';preferences.set_manual(state,'123',True)
            result={'games':[{'appid':'123','action':'set','before':None,'after':AFTER}]}
            ws=unittest.mock.Mock()
            with patch.object(a.live_steam,'connect',return_value=ws),patch.object(a.live_steam,'set_tool') as setter:
                self.assertIn('0 live',a.apply_live(Path(tmp),result,state))
                setter.assert_not_called()

    def test_ui_shows_native_and_manual_recommendation(self):
        import ui_daemon,preferences
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            a.save_json(state/'plan.json',{'games':[{'appid':'123','native':True,'manual':False,'recommendation_known':True},{'appid':'456','manual':False,'recommended':AFTER,'recommendation_known':True}], 'tools':{'proton_10':{'display':'Proton 10.0'}}})
            preferences.set_manual(state,'456',True)
            payload=ui_daemon.payload(state)['games']
            self.assertEqual(payload['123']['choice'],'Native Linux')
            self.assertEqual(payload['456']['choice'],'Proton 10.0');self.assertTrue(payload['456']['manual'])
