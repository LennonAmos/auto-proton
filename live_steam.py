"""Transient loopback-only Steam UI calls. No persistent hooks or page scripts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import time
import urllib.parse
import urllib.request


class WebSocket:
    def __init__(self, url, port):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or parsed.port != port or not re.fullmatch(r'/devtools/page/[A-Za-z0-9-]+', parsed.path):
            raise ValueError('Refusing non-local Steam debugger endpoint')
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=8)
        self.sock.settimeout(8)
        self.buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET {parsed.path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk or len(self.buffer) > 65536:
                self.close()
                raise ValueError('Invalid debugger handshake')
            self.buffer.extend(chunk)
        header, rest = bytes(self.buffer).split(b'\r\n\r\n', 1)
        self.buffer = bytearray(rest)
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        fields = dict(line.decode().split(':', 1) for line in header.split(b'\r\n')[1:] if b':' in line)
        if b' 101 ' not in header.split(b'\r\n')[0] or next((v.strip() for k,v in fields.items() if k.lower()=='sec-websocket-accept'), '') != expected:
            self.close()
            raise ValueError('Steam debugger rejected connection')
        self.ident = 0

    def close(self):
        self.sock.close()

    def read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(min(65536, n-len(self.buffer)))
            if not chunk:
                raise ValueError('Steam debugger disconnected')
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:n])
        del self.buffer[:n]
        return result

    def send(self, payload, opcode=1):
        mask = os.urandom(4)
        size = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126])+struct.pack('!H',size) if size < 65536 else bytes([0x80 | 127])+struct.pack('!Q',size)
        self.sock.sendall(head+mask+bytes(c ^ mask[i%4] for i,c in enumerate(payload)))

    def receive(self):
        out = bytearray()
        while True:
            first, second = self.read(2)
            length = second & 127
            if length == 126: length = struct.unpack('!H',self.read(2))[0]
            elif length == 127: length = struct.unpack('!Q',self.read(8))[0]
            if length > 8_000_000 or len(out)+length > 8_000_000:
                raise ValueError('Steam debugger response too large')
            mask = self.read(4) if second & 128 else None
            payload = self.read(length)
            if mask: payload = bytes(c ^ mask[i%4] for i,c in enumerate(payload))
            opcode = first & 15
            if opcode == 8: raise ValueError('Steam debugger closed connection')
            if opcode == 9:
                self.send(payload,10)
                continue
            if opcode == 10: continue
            if opcode not in (0,1): raise ValueError('Unexpected debugger frame')
            out.extend(payload)
            if first & 128: return json.loads(out)

    def evaluate(self, expression):
        self.ident += 1
        ident = self.ident
        self.send(json.dumps({'id':ident,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True,'awaitPromise':True,'timeout':6000}}).encode())
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            response = self.receive()
            if response.get('id') != ident: continue
            if 'error' in response or 'exceptionDetails' in response.get('result',{}):
                raise ValueError('Steam UI interface unavailable: '+str(response.get('error') or response['result']['exceptionDetails'].get('text')))
            return response['result']['result'].get('value')
        raise ValueError('Steam debugger timed out')


def connect(root):
    """Use only this user's Steam webhelper and this Steam installation."""
    ports = set()
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid(): continue
            args = (proc/'cmdline').read_bytes().decode(errors='replace').split('\0')
            if not args or Path(args[0]).name != 'steamwebhelper': continue
            paths = [arg.split('=',1)[1] for arg in args if arg.startswith(('-steampath=', '-clientui='))]
            if not any(Path(path).resolve().is_relative_to(root.resolve()) for path in paths): continue
            for arg in args:
                if arg.startswith('--remote-debugging-port='): ports.add(int(arg.split('=',1)[1]))
        except (OSError, ValueError): continue
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in sorted(ports):
        try:
            with opener.open(f'http://127.0.0.1:{port}/json/list',timeout=3) as response:
                pages = json.loads(response.read(1_000_000))
            for page in pages:
                # Never evaluate scripts in store, community, or arbitrary browser content.
                url = urllib.parse.urlsplit(page.get('url',''))
                if page.get('title') != 'SharedJSContext' or url.hostname != 'steamloopback.host' or url.path != '/index.html': continue
                ws = WebSocket(page['webSocketDebuggerUrl'],port)
                if ws.evaluate("typeof SteamClient !== 'undefined' && typeof SteamClient.Apps.SpecifyCompatTool === 'function'"):
                    return ws
                ws.close()
        except (OSError,ValueError,KeyError,TypeError): continue
    raise ValueError('Steam live UI connection unavailable. Enable CEF debugging during setup and restart Steam normally once; no live file writes attempted.')


# Steam's cached appDetailsStore can be stale for titles outside the visible page.
# Keep a short-lived native subscription and always unregister it, including timeouts.
READ_DETAILS = """(id) => new Promise((resolve,reject) => {
  let sub, done = false;
  const cleanup = () => { clearTimeout(timer); if (sub) sub.unregister(); };
  const timer = setTimeout(() => { done=true; cleanup(); reject(new Error('Steam details timed out')); },3000);
  sub = SteamClient.Apps.RegisterForAppDetails(id, d => {
    if (done) return;
    if (!d || typeof d.strCompatToolName !== 'string') return;
    done=true; cleanup(); resolve({name:d.strCompatToolName, priority:d.nCompatToolPriority});
  });
  if (done) cleanup();
})"""


def snapshot(ws, appids):
    ids = [int(k) for k in appids]
    return ws.evaluate('(async () => { const read = '+READ_DETAILS+'; const ids = '+json.dumps(ids)+'; const data = await Promise.all(ids.map(read)); return Object.fromEntries(ids.map((id,i)=>[id,data[i]])); })()')


def set_tool(ws, appid, name):
    if not str(appid).isdecimal() or int(appid) <= 0:
        raise ValueError('Invalid live app ID')
    return ws.evaluate("""new Promise((resolve,reject) => {
      const id = APPID, name = TOOL;
      let sub, done = false, sent = false;
      const cleanup = () => { clearTimeout(timer); if (sub) sub.unregister(); };
      const timer = setTimeout(() => { done=true; cleanup(); reject(new Error('Steam did not confirm compatibility setting')); },4000);
      sub = SteamClient.Apps.RegisterForAppDetails(id, d => {
        if (done || !sent || !d) return;
        if (name ? d.strCompatToolName === name && d.nCompatToolPriority === 250 : d.nCompatToolPriority < 250) {
          done=true; cleanup(); resolve(true);
        }
      });
      sent=true;
      try { SteamClient.Apps.SpecifyCompatTool(id,name); }
      catch (e) { done=true; cleanup(); reject(e); }
      if (done) cleanup();
    })""".replace('APPID',str(int(appid))).replace('TOOL',json.dumps(name)))
