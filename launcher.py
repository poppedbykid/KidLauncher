"""
KidLauncher - Flask backend
Microsoft auth, mod management, Fabric/Quilt install, ZGC JVM tuning.
"""
import sys
import os
import json
import base64
import threading
import subprocess
import time
import queue
import secrets
import hashlib
import webbrowser
import requests as req
import webview

from flask import Flask, request, jsonify, send_from_directory, Response, redirect
from urllib.parse import urlencode, urlparse, parse_qs

import minecraft_launcher_lib as mll
from minecraft_launcher_lib.mod_loader import Fabric, Quilt

try:
    from pypresence import Presence
except ImportError:
    Presence = None

# ── Discord RPC ────────────────────────────────────────────────────────────────
RPC_CLIENT_ID = "1044582020235305020" # Generic Minecraft RPC ID
rpc = None

def init_rpc():
    global rpc
    if not Presence: return
    try:
        rpc = Presence(RPC_CLIENT_ID)
        rpc.connect()
        update_rpc("In Launcher", "Preparing to play")
    except Exception as e:
        print(f"[RPC] Failed to connect: {e}")
        rpc = None

def update_rpc(details, state, start_time=None):
    global rpc
    if not rpc: return
    try:
        rpc.update(
            details=details,
            state=state,
            start=start_time or int(time.time()),
            large_image="minecraft", # Generic Minecraft block asset
            large_text="KidLauncher",
            small_image="logo",
            small_text="Made by poppedbykid"
        )
    except Exception as e:
        print(f"[RPC] Update failed: {e}")



# ── Paths ─────────────────────────────────────────────────────────────────────
def get_base():
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

BASE      = get_base()
DATA_DIR  = os.path.join(os.path.expanduser('~'), '.solar_launcher')
MC_ROOT   = os.path.join(DATA_DIR, '.minecraft')
INST_DIR  = os.path.join(MC_ROOT, 'instances')
ACCT_FILE = os.path.join(DATA_DIR, 'accounts.json')  # Token storage
os.makedirs(MC_ROOT,  exist_ok=True)
os.makedirs(INST_DIR, exist_ok=True)

def mods_dir(instance):
    d = os.path.join(INST_DIR, instance, 'mods')
    os.makedirs(d, exist_ok=True)
    return d

def game_dir(instance):
    d = os.path.join(INST_DIR, instance)
    os.makedirs(os.path.join(d, 'mods'), exist_ok=True)
    return d

def list_mods(instance):
    return sorted([
        f for f in os.listdir(mods_dir(instance))
        if f.endswith('.jar') or f.endswith('.jar.disabled')
    ])

# ── Account storage ────────────────────────────────────────────────────────────
# accounts.json structure:
# {
#   "active": "uuid-here",
#   "accounts": {
#     "uuid": {
#       "username": "Steve",
#       "uuid": "...",
#       "access_token": "...",        <- Minecraft JWT (expires ~24h)
#       "refresh_token": "...",       <- Microsoft refresh (long-lived)
#       "token_expiry": 1234567890,   <- Unix timestamp
#       "type": "microsoft" | "offline"
#     }
#   }
# }

def load_accounts():
    if os.path.exists(ACCT_FILE):
        try:
            with open(ACCT_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {'active': None, 'accounts': {}}

def save_accounts(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ACCT_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def get_active_account():
    data = load_accounts()
    uid  = data.get('active')
    if uid and uid in data['accounts']:
        return data['accounts'][uid]
    return None

def token_needs_refresh(acct):
    """Returns True if the Minecraft access token is expired or expires soon."""
    expiry = acct.get('token_expiry', 0)
    return time.time() > expiry - 300  # refresh 5 min before expiry

def refresh_microsoft_token(acct, client_id):
    """Use the stored refresh_token to get new MS + MC tokens silently."""
    try:
        # Step 1: Refresh Microsoft token
        r = req.post(
            'https://login.microsoftonline.com/consumers/oauth2/v2.0/token',
            data={
                'client_id':     client_id,
                'grant_type':    'refresh_token',
                'refresh_token': acct['refresh_token'],
                'scope':         'XboxLive.signin offline_access',
            }, timeout=15
        )
        r.raise_for_status()
        ms_data = r.json()

        # Step 2: Xbox Live
        xbl = req.post('https://user.auth.xboxlive.com/user/authenticate', json={
            'Properties': {'AuthMethod': 'RPS', 'SiteName': 'user.auth.xboxlive.com',
                           'RpsTicket': f"d={ms_data['access_token']}"},
            'RelyingParty': 'http://auth.xboxlive.com', 'TokenType': 'JWT'
        }, headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, timeout=15)
        xbl.raise_for_status()
        xbl_data = xbl.json()
        xbl_token = xbl_data['Token']
        uhs       = xbl_data['DisplayClaims']['xui'][0]['uhs']

        # Step 3: XSTS
        xsts = req.post('https://xsts.auth.xboxlive.com/xsts/authorize', json={
            'Properties': {'SandboxId': 'RETAIL', 'UserTokens': [xbl_token]},
            'RelyingParty': 'rp://api.minecraftservices.com/', 'TokenType': 'JWT'
        }, headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, timeout=15)
        xsts.raise_for_status()
        xsts_token = xsts.json()['Token']

        # Step 4: Minecraft token
        mc = req.post('https://api.minecraftservices.com/authentication/login_with_xbox', json={
            'identityToken': f'XBL3.0 x={uhs};{xsts_token}'
        }, timeout=15)
        mc.raise_for_status()
        mc_data = mc.json()

        # Update stored account
        acct['access_token']  = mc_data['access_token']
        acct['token_expiry']  = time.time() + mc_data.get('expires_in', 86400)
        acct['refresh_token'] = ms_data.get('refresh_token', acct['refresh_token'])

        data = load_accounts()
        data['accounts'][acct['uuid']] = acct
        save_accounts(data)
        return acct
    except Exception as e:
        push_log(f'[AUTH] Token refresh failed: {e}')
        return None

# Temp storage for OAuth state during login flow
_oauth_state = {}

# ── Java detection ─────────────────────────────────────────────────────────────
def find_java():
    # 1. Use Minecraft's own bundled Java 21 runtime (same as Modrinth/Prism)
    mc_runtimes = [
        os.path.join(MC_ROOT, 'runtime', 'java-runtime-gamma', 'windows-x64', 'java-runtime-gamma', 'bin', 'java.exe'),
        os.path.join(MC_ROOT, 'runtime', 'java-runtime-delta', 'windows-x64', 'java-runtime-delta', 'bin', 'java.exe'),
        os.path.join(MC_ROOT, 'runtime', 'java-runtime-beta', 'windows-x64', 'java-runtime-beta', 'bin', 'java.exe'),
    ]
    for p in mc_runtimes:
        if os.path.exists(p):
            return p

    # 2. Scan for system Java — glob all Adoptium/MS installs to find any version
    import glob
    patterns = [
        r'C:\Program Files\Eclipse Adoptium\jdk-21*\bin\java.exe',
        r'C:\Program Files\Eclipse Adoptium\jdk-17*\bin\java.exe',
        r'C:\Program Files\Microsoft\jdk-21*\bin\java.exe',
        r'C:\Program Files\Microsoft\jdk-17*\bin\java.exe',
        r'C:\Program Files\Java\jdk-21*\bin\java.exe',
        r'C:\Program Files\Java\jdk-17*\bin\java.exe',
        r'C:\Program Files\Java\jdk-11*\bin\java.exe',
    ]
    for pat in patterns:
        found = sorted(glob.glob(pat), reverse=True)  # highest version first
        if found:
            return found[0]

    # 3. PATH fallback
    return 'java'

# ── JVM args ───────────────────────────────────────────────────────────────────
def build_jvm_args(ram_gb):
    r = f'{ram_gb}G'
    # Detect Java version to pick best GC
    java = find_java()
    try:
        import subprocess as _sp
        out = _sp.check_output([java, '-version'], stderr=_sp.STDOUT, text=True, timeout=5)
        is_java21 = '"21.' in out or '"22.' in out or '"23.' in out
    except Exception:
        is_java21 = False

    base = [
        f'-Xmx{r}', f'-Xms{r}',
        '-XX:+UnlockExperimentalVMOptions',
        '-XX:+UnlockDiagnosticVMOptions',
        '-XX:+AlwaysPreTouch',          # Pre-allocate memory to prevent mid-game page faults
        '-XX:+DisableExplicitGC',       # Stop mods from forcing GC pauses
        '-XX:+PerfDisableSharedMem',    # Reduce disk I/O lag
        '-XX:+ParallelRefProcEnabled',
        '-XX:MaxTenuringThreshold=1',
        '-XX:MaxGCPauseMillis=10',      # Aggressive target for smoothness
        '-XX:+UseStringDeduplication',  # Save RAM by merging identical text strings
        '-XX:+UseFastAccessorMethods',  # Optimize getter/setter calls
        '-XX:+OptimizeStringConcat',    # Faster string operations
        '-XX:+UseVectorizedMismatchIntrinsic', # Hardware-accelerated comparisons
        '-XX:MaxInlineLevel=15',        # More aggressive method inlining
        '-Dsun.io.useCanonPrefixCache=false',
        '-Djava.net.preferIPv4Stack=true',
        '-Dfile.encoding=UTF-8',
        '-Dsun.java2d.opengl=true',     # Hardware acceleration for some UI elements
    ]

    if is_java21:
        # Java 21: Generational ZGC — The gold standard for zero-stutter gaming
        gc_flags = [
            '-XX:+UseZGC',
            '-XX:+ZGenerational',
            '-XX:ZUncommitDelay=60',
            '-XX:ZCollectionInterval=5',
        ]
    else:
        # Java 17: Highly optimized G1GC (Aikar's Flags + Improvements)
        gc_flags = [
            '-XX:+UseG1GC',
            '-XX:G1NewSizePercent=30',
            '-XX:G1MaxNewSizePercent=40',
            '-XX:G1HeapRegionSize=16M',
            '-XX:G1ReservePercent=20',
            '-XX:G1HeapWastePercent=5',
            '-XX:G1MixedGCCountTarget=4',
            '-XX:InitiatingHeapOccupancyPercent=15',
            '-XX:G1MixedGCLiveThresholdPercent=90',
            '-XX:G1RSetUpdatingPauseTimePercent=5',
            '-XX:SurvivorRatio=32',
        ]

    return base + gc_flags


# ── Remote blacklist ───────────────────────────────────────────────────────────
# Edit your GitHub Gist (raw URL) to add/remove banned users.
# Format of the Gist JSON:
# { "blacklisted": [{"uuid":"...","username":"Steve","reason":"cheating"}] }

def get_blacklist_url():
    cfg = os.path.join(DATA_DIR, 'settings.json')
    try:
        return json.load(open(cfg)).get('blacklistUrl', '')
    except Exception:
        return ''

def check_blacklist(username, uuid):
    """Returns (banned: bool, reason: str)"""
    url = get_blacklist_url()
    if not url:
        return False, ''
    try:
        r = req.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        for entry in data.get('blacklisted', []):
            if entry.get('uuid') == uuid or entry.get('username','').lower() == username.lower():
                return True, entry.get('reason', 'Banned')
    except Exception as e:
        push_log(f'[BL] Could not fetch blacklist: {e} — allowing launch')
    return False, ''

# ── SSE event queue ────────────────────────────────────────────────────────────
event_queue = queue.Queue()

def push_event(evt_type, data):
    event_queue.put(json.dumps({'type': evt_type, 'data': data}))

def push_log(msg):      push_event('log', str(msg))
def push_progress(d):   push_event('progress', d)
def push_mods(inst, m): push_event('mods', {'instance': inst, 'mods': m})

# ── mll callback factory ───────────────────────────────────────────────────────
def make_cb():
    return {
        'setStatus':   lambda s: push_log(f'[INSTALL] {s}'),
        'setProgress': lambda v: push_progress({'type': 'assets', 'current': v, 'total': 100}),
        'setMax':      lambda _: None,
    }

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024  # 512 MB max upload

@app.route('/')
def index():
    return send_from_directory(os.path.join(BASE, 'ui'), 'index.html')

@app.route('/ui/<path:filename>')
def ui_static(filename):
    return send_from_directory(os.path.join(BASE, 'ui'), filename)

# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route('/api/auth/status')
def auth_status():
    """Returns current logged-in account info."""
    acct = get_active_account()
    if not acct:
        return jsonify({'loggedIn': False})
    return jsonify({
        'loggedIn': True,
        'username': acct['username'],
        'uuid':     acct['uuid'],
        'type':     acct.get('type', 'offline'),
    })

@app.route('/api/auth/accounts')
def auth_accounts():
    """Returns all stored accounts."""
    data = load_accounts()
    accounts = []
    for uid, acct in data['accounts'].items():
        accounts.append({
            'uuid':     uid,
            'username': acct['username'],
            'type':     acct.get('type', 'offline'),
            'active':   uid == data.get('active'),
        })
    return jsonify({'accounts': accounts, 'active': data.get('active')})

@app.route('/api/auth/switch', methods=['POST'])
def auth_switch():
    """Switch active account."""
    uid  = request.json.get('uuid')
    data = load_accounts()
    if uid in data['accounts']:
        data['active'] = uid
        save_accounts(data)
        push_event('auth', {'loggedIn': True, 'username': data['accounts'][uid]['username'],
                            'uuid': uid, 'type': data['accounts'][uid].get('type','offline')})
    return jsonify({'ok': True})

@app.route('/api/auth/remove', methods=['POST'])
def auth_remove():
    """Remove an account."""
    uid  = request.json.get('uuid')
    data = load_accounts()
    if uid in data['accounts']:
        del data['accounts'][uid]
        if data.get('active') == uid:
            data['active'] = next(iter(data['accounts']), None)
    save_accounts(data)
    return jsonify({'ok': True})

@app.route('/api/auth/login/microsoft', methods=['POST'])
def auth_login_microsoft():
    """Start Microsoft OAuth PKCE flow. Returns the URL to open."""
    client_id = request.json.get('clientId', '').strip()
    if not client_id:
        return jsonify({'error': 'No client_id provided. See Settings > Azure App.'}), 400

    PORT = 29512
    redirect_uri = f'http://localhost:{PORT}/auth/callback'

    # PKCE
    code_verifier  = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b'=').decode()
    state = secrets.token_hex(16)

    _oauth_state['verifier']     = code_verifier
    _oauth_state['state']        = state
    _oauth_state['client_id']    = client_id
    _oauth_state['redirect_uri'] = redirect_uri

    params = {
        'client_id':             client_id,
        'response_type':         'code',
        'redirect_uri':          redirect_uri,
        'scope':                 'XboxLive.signin offline_access',
        'state':                 state,
        'code_challenge':        code_challenge,
        'code_challenge_method': 'S256',
        'prompt':                'select_account',
    }
    url = 'https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?' + urlencode(params)
    push_log('[AUTH] Opening Microsoft login...')
    return jsonify({'url': url})

@app.route('/auth/callback')
def auth_callback():
    """Microsoft redirects here after login. Exchange code for tokens."""
    code  = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    if error:
        push_log(f'[AUTH] Login failed: {error}')
        return '<script>window.close();</script><p>Login failed: ' + error + '</p>'

    if state != _oauth_state.get('state'):
        return '<script>window.close();</script><p>Invalid state. Please try again.</p>', 400

    client_id    = _oauth_state['client_id']
    redirect_uri = _oauth_state['redirect_uri']
    verifier     = _oauth_state['verifier']

    def _do_auth():
        try:
            push_log('[AUTH] Exchanging code for tokens...')

            # Step 1: Exchange auth code for MS tokens
            r = req.post(
                'https://login.microsoftonline.com/consumers/oauth2/v2.0/token',
                data={
                    'client_id':     client_id,
                    'code':          code,
                    'redirect_uri':  redirect_uri,
                    'grant_type':    'authorization_code',
                    'code_verifier': verifier,
                }, timeout=15
            )
            r.raise_for_status()
            ms_data = r.json()

            # Step 2: Xbox Live auth
            xbl = req.post('https://user.auth.xboxlive.com/user/authenticate', json={
                'Properties': {'AuthMethod': 'RPS', 'SiteName': 'user.auth.xboxlive.com',
                               'RpsTicket': f"d={ms_data['access_token']}"},
                'RelyingParty': 'http://auth.xboxlive.com', 'TokenType': 'JWT'
            }, headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, timeout=15)
            xbl.raise_for_status()
            xbl_data  = xbl.json()
            xbl_token = xbl_data['Token']
            uhs       = xbl_data['DisplayClaims']['xui'][0]['uhs']

            # Step 3: XSTS token
            xsts = req.post('https://xsts.auth.xboxlive.com/xsts/authorize', json={
                'Properties': {'SandboxId': 'RETAIL', 'UserTokens': [xbl_token]},
                'RelyingParty': 'rp://api.minecraftservices.com/', 'TokenType': 'JWT'
            }, headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, timeout=15)
            xsts.raise_for_status()
            xsts_token = xsts.json()['Token']

            # Step 4: Minecraft token
            mc = req.post('https://api.minecraftservices.com/authentication/login_with_xbox',
                json={'identityToken': f'XBL3.0 x={uhs};{xsts_token}'}, timeout=15)
            mc.raise_for_status()
            mc_data = mc.json()

            # Step 5: Get Minecraft profile (username + UUID)
            profile = req.get('https://api.minecraftservices.com/minecraft/profile',
                headers={'Authorization': f"Bearer {mc_data['access_token']}"}, timeout=15)

            if profile.status_code == 404:
                push_log('[AUTH] ❌ This Microsoft account does not own Minecraft!')
                push_event('auth', {'error': 'Account does not own Minecraft'})
                return

            profile.raise_for_status()
            prof = profile.json()

            # Step 6: Save account
            acct = {
                'username':      prof['name'],
                'uuid':          prof['id'],
                'access_token':  mc_data['access_token'],
                'refresh_token': ms_data.get('refresh_token', ''),
                'token_expiry':  time.time() + mc_data.get('expires_in', 86400),
                'type':          'microsoft',
                'client_id':     client_id,
            }

            data = load_accounts()
            data['accounts'][prof['id']] = acct
            data['active'] = prof['id']
            save_accounts(data)

            push_log(f"[AUTH] ✅ Logged in as: {prof['name']}")
            push_event('auth', {
                'loggedIn': True,
                'username': prof['name'],
                'uuid':     prof['id'],
                'type':     'microsoft',
            })

        except Exception as e:
            import traceback
            push_log(f'[AUTH] ❌ Login error: {e}')
            push_log(traceback.format_exc())
            push_event('auth', {'error': str(e)})

    threading.Thread(target=_do_auth, daemon=True).start()
    return '''<html><head><style>body{font-family:sans-serif;background:#0c0c0f;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}</style></head>
    <body><div style="text-align:center"><h2 style="color:#7c5cfc">✅ Logging in...</h2><p>You can close this window.</p><script>setTimeout(()=>window.close(),2000);</script></div></body></html>'''

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """Remove active account."""
    data = load_accounts()
    uid  = data.get('active')
    if uid and uid in data['accounts']:
        del data['accounts'][uid]
    data['active'] = next(iter(data['accounts']), None)
    save_accounts(data)
    push_event('auth', {'loggedIn': False})
    push_log('[AUTH] Logged out.')
    return jsonify({'ok': True})

@app.route('/api/settings/save', methods=['POST'])
def settings_save():
    """Save launcher settings (client_id etc)."""
    d = request.json
    cfg_path = os.path.join(DATA_DIR, 'settings.json')
    try:
        existing = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    except Exception:
        existing = {}
    existing.update(d)
    with open(cfg_path, 'w') as f:
        json.dump(existing, f, indent=2)
    return jsonify({'ok': True})

@app.route('/api/dev/check')
def dev_check():
    """Returns devMode=True only if settings.json has the correct devToken."""
    cfg_path = os.path.join(DATA_DIR, 'settings.json')
    try:
        cfg = json.load(open(cfg_path))
        return jsonify({'dev': cfg.get('devMode', False) is True})
    except Exception:
        return jsonify({'dev': False})

@app.route('/api/settings/load')
def settings_load():
    cfg_path = os.path.join(DATA_DIR, 'settings.json')
    try:
        return jsonify(json.load(open(cfg_path)))
    except Exception:
        return jsonify({})


@app.route('/events')
def events():
    def stream():
        while True:
            try:
                msg = event_queue.get(timeout=30)
                yield f'data: {msg}\n\n'
            except queue.Empty:
                yield ': heartbeat\n\n'
    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/add_instance', methods=['POST'])
def api_add_instance():
    d = request.json
    os.makedirs(mods_dir(d['name']), exist_ok=True)
    os.makedirs(os.path.join(INST_DIR, d['name'], 'saves'), exist_ok=True)
    push_log(f"[INSTANCE] Created: {d['name']} ({d['loader']} {d['version']})")
    return jsonify({'ok': True})

@app.route('/api/list_mods', methods=['POST'])
def api_list_mods():
    instance = request.json['instance']
    mods = list_mods(instance)
    push_mods(instance, mods)
    return jsonify({'mods': mods})

@app.route('/api/inject_mod', methods=['POST'])
def api_inject_mod():
    d        = request.json
    instance = d['instance']
    filename = d['filename']
    dest     = os.path.join(mods_dir(instance), filename)
    with open(dest, 'wb') as f:
        f.write(base64.b64decode(d['data']))
    push_log(f'[MOD] Injected: {filename}')
    push_mods(instance, list_mods(instance))
    return jsonify({'ok': True})

@app.route('/api/upload_mods', methods=['POST'])
def api_upload_mods():
    """Multipart file upload - handles large JARs without base64 overhead."""
    instance = request.form.get('instance')
    if not instance:
        return jsonify({'error': 'No instance specified'}), 400
    files = request.files.getlist('mods')
    injected = []
    for f in files:
        if f.filename.endswith('.jar'):
            dest = os.path.join(mods_dir(instance), f.filename)
            f.save(dest)
            injected.append(f.filename)
            push_log(f'[MOD] Uploaded: {f.filename}')
    push_mods(instance, list_mods(instance))
    return jsonify({'ok': True, 'injected': injected})

@app.route('/api/remove_mod', methods=['POST'])
def api_remove_mod():
    d        = request.json
    instance = d['instance']
    target   = os.path.join(mods_dir(instance), d['filename'])
    if os.path.exists(target):
        os.remove(target)
    push_mods(instance, list_mods(instance))
    return jsonify({'ok': True})

@app.route('/api/toggle_mod', methods=['POST'])
def api_toggle_mod():
    d        = request.json
    instance = d['instance']
    filename = d['filename']
    src      = os.path.join(mods_dir(instance), filename)
    dst      = src[:-9] if filename.endswith('.disabled') else src + '.disabled'
    if os.path.exists(src):
        os.rename(src, dst)
    push_mods(instance, list_mods(instance))
    return jsonify({'ok': True})

@app.route('/api/open_folder', methods=['POST'])
def api_open_folder():
    instance = request.json['instance']
    os.startfile(mods_dir(instance))
    return jsonify({'ok': True})

@app.route('/api/install_mod', methods=['POST'])
def api_install_mod():
    d        = request.json
    instance = d['instance']
    mod_id   = d['modId']
    mc_ver   = d.get('mcVersion', '1.20.1')
    loader   = d.get('loader', 'fabric')

    def _run():
        try:
            push_log(f'[MODRINTH] Resolving {mod_id}...')
            resp = req.get(f'https://api.modrinth.com/v2/project/{mod_id}/version', timeout=15)
            resp.raise_for_status()
            versions = resp.json()

            match = next(
                (v for v in versions
                 if mc_ver in v.get('game_versions', [])
                 and loader.lower() in [l.lower() for l in v.get('loaders', [])]),
                None
            )
            if not match:
                match = next((v for v in versions if mc_ver in v.get('game_versions', [])),
                             versions[0] if versions else None)
            if not match or not match.get('files'):
                push_log('[MODRINTH] No compatible file found.'); return

            fi    = match['files'][0]
            fname = fi['filename']
            dest  = os.path.join(mods_dir(instance), fname)
            push_log(f'[MODRINTH] Downloading: {fname}')

            with req.get(fi['url'], stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get('Content-Length', 0))
                done  = 0
                with open(dest, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            push_progress({'type': 'download-status', 'name': fname,
                                           'current': done, 'total': total or done})

            push_progress({'type': 'download-finished'})
            push_log(f'[MODRINTH] Installed: {fname}')

            # Auto-install dependencies
            for dep in match.get('dependencies', []):
                if dep.get('dependency_type') == 'required' and dep.get('project_id'):
                    dep_id = dep['project_id']
                    push_log(f'[DEPS] Auto-installing dependency: {dep_id}')
                    try:
                        dep_vers = req.get(
                            f'https://api.modrinth.com/v2/project/{dep_id}/version',
                            params={'game_versions': json.dumps([mc_ver]),
                                    'loaders': json.dumps([loader.lower()])},
                            timeout=15
                        ).json()
                        if dep_vers:
                            dfi = dep_vers[0]['files'][0]
                            ddest = os.path.join(mods_dir(instance), dfi['filename'])
                            if not os.path.exists(ddest):
                                with req.get(dfi['url'], stream=True, timeout=60) as dr:
                                    dr.raise_for_status()
                                    with open(ddest, 'wb') as df:
                                        for chunk in dr.iter_content(chunk_size=65536):
                                            if chunk: df.write(chunk)
                                push_log(f"[DEPS] ✅ Installed: {dfi['filename']}")
                    except Exception as de:
                        push_log(f'[DEPS] Failed to install {dep_id}: {de}')

            push_mods(instance, list_mods(instance))
        except Exception as e:
            push_log(f'[MODRINTH ERROR] {e}')

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True})

# ── Mod update checker ─────────────────────────────────────────────────────────
@app.route('/api/check_updates', methods=['POST'])
def api_check_updates():
    """Hash all installed JARs and check Modrinth for newer versions."""
    d        = request.json
    instance = d['instance']
    mc_ver   = d.get('mcVersion', '1.20.1')
    loader   = d.get('loader', 'fabric').lower()

    def _run():
        import hashlib
        mdir = mods_dir(instance)
        jars = [f for f in os.listdir(mdir) if f.endswith('.jar')]
        if not jars:
            push_log('[UPDATES] No mods to check.'); return

        push_log(f'[UPDATES] Checking {len(jars)} mods for updates...')
        hashes = {}
        for fname in jars:
            path = os.path.join(mdir, fname)
            try:
                sha1 = hashlib.sha1(open(path,'rb').read()).hexdigest()
                hashes[sha1] = fname
            except Exception:
                pass

        try:
            # Modrinth hash lookup — identifies ANY mod by its SHA1
            resp = req.post(
                'https://api.modrinth.com/v2/version_files',
                json={'hashes': list(hashes.keys()), 'algorithm': 'sha1'},
                timeout=20
            )
            resp.raise_for_status()
            found = resp.json()  # {sha1: version_info}

            updates = []
            for sha1, ver_info in found.items():
                proj_id = ver_info.get('project_id')
                cur_ver = ver_info.get('version_number', '?')
                fname   = hashes.get(sha1, sha1[:8])

                # Get latest version for this mc_ver+loader
                latest_resp = req.get(
                    f'https://api.modrinth.com/v2/project/{proj_id}/version',
                    params={'game_versions': json.dumps([mc_ver]),
                            'loaders':       json.dumps([loader])},
                    timeout=10
                )
                if latest_resp.ok and latest_resp.json():
                    latest = latest_resp.json()[0]
                    if latest['version_number'] != cur_ver:
                        updates.append({
                            'filename':    fname,
                            'project_id':  proj_id,
                            'current':     cur_ver,
                            'latest':      latest['version_number'],
                            'file_url':    latest['files'][0]['url'],
                            'file_name':   latest['files'][0]['filename'],
                        })

            if updates:
                push_log(f'[UPDATES] Found {len(updates)} update(s)!')
                push_event('updates_available', {'instance': instance, 'updates': updates})
            else:
                push_log('[UPDATES] ✅ All mods are up to date!')
                push_event('updates_available', {'instance': instance, 'updates': []})

        except Exception as e:
            push_log(f'[UPDATES ERROR] {e}')

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/apply_update', methods=['POST'])
def api_apply_update():
    """Download the updated mod and replace the old one."""
    d        = request.json
    instance = d['instance']
    old_file = d['oldFilename']
    new_url  = d['fileUrl']
    new_name = d['fileName']

    def _run():
        try:
            # Remove old
            old_path = os.path.join(mods_dir(instance), old_file)
            if os.path.exists(old_path): os.remove(old_path)
            # Download new
            dest = os.path.join(mods_dir(instance), new_name)
            with req.get(new_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk: f.write(chunk)
            push_log(f'[UPDATE] ✅ Updated: {old_file} → {new_name}')
            push_mods(instance, list_mods(instance))
        except Exception as e:
            push_log(f'[UPDATE ERROR] {e}')

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True})

# ── Modpack import (.mrpack) ───────────────────────────────────────────────────
@app.route('/api/import_modpack', methods=['POST'])
def api_import_modpack():
    """Import a Modrinth .mrpack modpack file."""
    if 'pack' not in request.files:
        return jsonify({'error': 'No file'}), 400
    pack_file = request.files['pack']
    inst_name = request.form.get('instance', pack_file.filename.replace('.mrpack',''))

    def _run():
        import zipfile, io
        try:
            push_log(f'[PACK] Importing modpack: {pack_file.filename}')
            data = pack_file.read()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                manifest = json.loads(zf.read('modrinth.index.json'))

            mc_ver   = manifest.get('dependencies', {}).get('minecraft', '1.20.1')
            loader   = 'fabric' if 'fabric-loader' in manifest.get('dependencies', {}) else 'vanilla'
            files    = manifest.get('files', [])

            push_log(f'[PACK] MC {mc_ver} | {loader} | {len(files)} files')

            # Create instance
            gd = game_dir(inst_name)
            push_log(f'[PACK] Instance: {inst_name}')

            # Download all mod files
            for i, f in enumerate(files):
                url   = f['downloads'][0]
                fname = url.split('/')[-1].split('?')[0]
                dest  = os.path.join(mods_dir(inst_name), fname)
                push_progress({'type': 'assets', 'current': i+1, 'total': len(files)})
                push_log(f'[PACK] [{i+1}/{len(files)}] {fname}')
                try:
                    with req.get(url, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        with open(dest, 'wb') as df:
                            for chunk in r.iter_content(65536):
                                if chunk: df.write(chunk)
                except Exception as fe:
                    push_log(f'[PACK] Failed: {fname}: {fe}')

            push_progress({'type': 'download-finished'})
            push_log(f'[PACK] ✅ Modpack imported! Create instance: {inst_name} ({loader} {mc_ver})')
            push_event('modpack_imported', {'name': inst_name, 'version': mc_ver, 'loader': loader})

        except Exception as e:
            push_log(f'[PACK ERROR] {e}')

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/launch', methods=['POST'])
def api_launch():
    d         = request.json
    username  = d.get('username', 'Player')
    version   = d.get('version', '1.20.1')
    ram_gb    = int(str(d.get('ram', '4')).replace('G', '').strip())
    instance  = d.get('instance', '_default')
    loader_up = d.get('loader', 'VANILLA').upper()

    def _run():
        try:
            import warnings
            gdir      = game_dir(instance)
            java_path = find_java()
            push_log(f'[SOLAR] ⚡ {loader_up} {version} | RAM: {ram_gb}G')
            push_log(f'[SOLAR] Java: {java_path}')
            push_log(f'[SOLAR] Game dir: {gdir}')

            cb = make_cb()

            # Step 1: Install vanilla and verify JAR
            push_log(f'[INSTALL] Installing vanilla {version}...')
            mll.install.install_minecraft_version(version, MC_ROOT, callback=cb)

            vanilla_jar = os.path.join(MC_ROOT, 'versions', version, f'{version}.jar')
            if not os.path.exists(vanilla_jar) or os.path.getsize(vanilla_jar) < 1024:
                push_log(f'[ERROR] Vanilla JAR missing! Re-downloading...')
                mll.install.install_minecraft_version(version, MC_ROOT, callback=cb)

            push_log(f'[OK] Vanilla JAR: {os.path.getsize(vanilla_jar):,} bytes')

            version_id = version

            # Blacklist check — runs before any game launch
            _bl_user = (get_active_account() or {}).get('username', username)
            _bl_uuid = (get_active_account() or {}).get('uuid', '00000000-0000-0000-0000-000000000000')
            banned, reason = check_blacklist(_bl_user, _bl_uuid)
            if banned:
                push_log(f'[BANNED] ❌ {_bl_user} is banned: {reason}')
                push_event('banned', {'username': _bl_user, 'reason': reason})
                return

            # Step 2: Install mod loader
            if loader_up == 'FABRIC':
                push_log('[FABRIC] Installing Fabric loader...')
                # Use deprecated module — it's battle-tested (runs official installer)
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    import minecraft_launcher_lib.fabric as fmod
                    fmod.install_fabric(version, MC_ROOT, callback=cb, java=java_path)
                    lv = fmod.get_latest_loader_version()
                version_id = f'fabric-loader-{lv}-{version}'
                push_log(f'[FABRIC] Version ID: {version_id}')

                fabric_json = os.path.join(MC_ROOT, 'versions', version_id, f'{version_id}.json')
                if not os.path.exists(fabric_json):
                    push_log(f'[ERROR] Fabric JSON not created: {fabric_json}')
                    push_log('[ERROR] Is Java installed? Check java path above.')
                    return

                # Download Fabric libraries
                mll.install.install_minecraft_version(version_id, MC_ROOT, callback=cb)

            elif loader_up == 'QUILT':
                push_log('[QUILT] Installing Quilt...')
                quilt   = Quilt()
                loaders = quilt.get_loader_versions(version, stable_only=True)
                lv      = loaders[0] if loaders else None
                if lv:
                    quilt.install(version, MC_ROOT, cb, java_path, lv)
                    version_id = f'quilt-loader-{lv}-{version}'
                    mll.install.install_minecraft_version(version_id, MC_ROOT, callback=cb)

            # Step 3: Resolve account credentials
            acct = get_active_account()
            if acct and acct.get('type') == 'microsoft':
                # Auto-refresh token if expired
                if token_needs_refresh(acct):
                    push_log('[AUTH] Refreshing Microsoft token...')
                    cfg_path = os.path.join(DATA_DIR, 'settings.json')
                    try:
                        client_id = json.load(open(cfg_path)).get('clientId', '')
                    except Exception:
                        client_id = acct.get('client_id', '')
                    acct = refresh_microsoft_token(acct, client_id) or acct

                push_log(f"[AUTH] Launching as: {acct['username']} (Microsoft)")
                options = {
                    'username':        acct['username'],
                    'uuid':            acct['uuid'],
                    'token':           acct['access_token'],
                    'executablePath':  java_path,
                    'jvmArguments':    build_jvm_args(ram_gb),
                    'gameDirectory':   gdir,
                    'launcherName':    'KidLauncher',
                    'launcherVersion': '1.0',
                }
            else:
                # Offline mode
                _user = acct['username'] if acct else (username or 'Player')
                push_log(f'[AUTH] Launching offline as: {_user}')
                options = {
                    'username':        _user,
                    'uuid':            '00000000-0000-0000-0000-000000000000',
                    'token':           'null',
                    'executablePath':  java_path,
                    'jvmArguments':    build_jvm_args(ram_gb),
                    'gameDirectory':   gdir,
                    'launcherName':    'KidLauncher',
                    'launcherVersion': '1.0',
                }

            push_log(f'[SOLAR] Building command: {version_id}')
            cmd = mll.command.get_minecraft_command(version_id, MC_ROOT, options)
            push_log(f'[DEBUG] Executable: {cmd[0]}')

            push_log('[SOLAR] LAUNCHING...')
            push_progress({'type': 'ignition', 'current': 100, 'total': 100, 'step': 'GAME STARTING'})

            # Update Discord RPC
            update_rpc("Playing Kid Launcher | Minecraft", "Made by poppedbykid")

            proc = subprocess.Popen(
                cmd, cwd=gdir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            for line in proc.stdout:
                s = line.rstrip()
                if s: push_log(f'[MC] {s}')
            proc.wait()
            push_log(f'[SOLAR] Game exited: {proc.returncode}')
            push_progress({'type': 'download-finished'})
            
            # Reset Discord RPC
            update_rpc("In Launcher", "Preparing to play")


        except mll.exceptions.VersionNotFound as e:
            push_log(f'[ERROR] Version not found: {e} — launch again to retry.')
        except Exception as e:
            import traceback
            push_log(f'[FATAL] {type(e).__name__}: {e}')
            push_log(traceback.format_exc())

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True})

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    PORT = 29512
    url = f'http://127.0.0.1:{PORT}'
    
    # Start Flask in a background thread
    server_thread = threading.Thread(
        target=lambda: app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True, use_reloader=False),
        daemon=True
    )
    server_thread.start()
    
    print(f'[SOLAR] Server started at {url}')
    print(f'[SOLAR] MC root: {MC_ROOT}')
    
    # Create the application window
    window = webview.create_window(
        'KidLauncher', 
        url,
        width=1000, 
        height=680, 
        resizable=True,
        background_color='#07070d'
    )
    
    # Initialize Discord RPC
    threading.Thread(target=init_rpc, daemon=True).start()

    # Start the webview loop (this blocks until window is closed)
    webview.start()

