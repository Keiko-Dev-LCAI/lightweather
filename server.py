#!/usr/bin/env python3
"""LightWeather — Backend server. Weather data + AIVM forecast explanation + premium subscriptions."""

import os, time, json, threading, base64 as _b64_mod, secrets as _secrets_mod, sqlite3
import urllib.request as _urllib_req
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as _requests

app = Flask(__name__)
_CORS_ORIGINS = [o.strip() for o in __import__('os').environ.get(
    'CORS_ORIGINS',
    'https://lightweather.win,http://localhost:5000,http://127.0.0.1:5000'
).split(',') if o.strip()]
CORS(app, origins=_CORS_ORIGINS)

# ── AIVM abuse guards (open-endpoints audit 2026-08-22) ─────────────────────
from datetime import datetime, timezone as _tz
_CHAT_RATE_PER_MIN = int(os.environ.get("CHAT_RATE_PER_MIN", "5"))
_CHAT_RATE_PER_DAY = int(os.environ.get("CHAT_RATE_PER_DAY", "30"))
_DAILY_LCAI_CAP = float(os.environ.get("DAILY_LCAI_CAP", "50"))
_LCAI_PER_JOB = float(os.environ.get("LCAI_PER_JOB", "0.02"))
_MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_JOBS", "8"))
_rate_lock = threading.Lock(); _rate_hits = {}
_spend_lock = threading.Lock(); _spend_day = ""; _spend_jobs = 0
_active = 0; _active_lock = threading.Lock()

def _client_ip():
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or (request.remote_addr or "unknown")

def _gate_ai():
    global _spend_day, _spend_jobs, _active
    ip = _client_ip(); now = time.time(); day = datetime.now(_tz.utc).strftime("%Y-%m-%d")
    with _rate_lock:
        rec = _rate_hits.get(ip)
        if not rec or rec.get("day") != day:
            rec = {"day": day, "hits": []}; _rate_hits[ip] = rec
        hits = [t for t in rec["hits"] if now - t < 86400]
        if len([t for t in hits if now - t < 60]) >= _CHAT_RATE_PER_MIN:
            return False, 429, "Too many requests — wait a minute and try again."
        if len(hits) >= _CHAT_RATE_PER_DAY:
            return False, 429, "Daily limit reached — try again tomorrow."
        hits.append(now); rec["hits"] = hits
    with _active_lock:
        if _active >= _MAX_CONCURRENT:
            return False, 503, "AI is busy right now — give it a moment and try again."
        _active += 1
    with _spend_lock:
        if _spend_day != day: _spend_day = day; _spend_jobs = 0
        if _spend_jobs * _LCAI_PER_JOB >= _DAILY_LCAI_CAP:
            with _active_lock: _active = max(0, _active - 1)
            return False, 503, "AI is at capacity for today — please try again tomorrow."
        _spend_jobs += 1
    return True, 200, ""

def _ungate_ai():
    global _active
    with _active_lock: _active = max(0, _active - 1)

WEATHERAPI_KEY = os.environ.get("WEATHERAPI_KEY", "")
WEATHERAPI_BASE = "https://api.weatherapi.com/v1"
LCAI_RPC = "https://rpc.mainnet.lightchain.ai"

# ── PREMIUM CONFIG ──────────────────────────────────────────────────────────
OWNER_WALLET       = '0x6518fd07b3da01b17bd37d7c40f9a5e3c87a09ba'   # receives subscription fees
MONTHLY_PRICE_USD  = 0.50                                             # $0.50/month
PREMIUM_WHITELIST  = {'0x729fea1d8ca343f26c4cc743a4e1898d65ce6a76'}  # dApp wallet always free

# ── DATABASE ────────────────────────────────────────────────────────────────
_data_dir = os.environ.get('DATA_DIR', '/app/data')
os.makedirs(_data_dir, exist_ok=True)
DB_PATH = os.path.join(_data_dir, 'lightweather.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        wallet TEXT PRIMARY KEY,
        expires_at INTEGER NOT NULL,
        tx_hash TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

# ── LCAI PRICE FEED (cached 5 min) ──────────────────────────────────────────
_lcai_price_cache = {'price': 0.004, 'ts': 0}

def get_lcai_price():
    global _lcai_price_cache
    now = time.time()
    if now - _lcai_price_cache['ts'] < 300:
        return _lcai_price_cache['price']
    # Try CoinGecko
    try:
        req = _urllib_req.Request(
            'https://api.coingecko.com/api/v3/simple/price?ids=lightchain-ai&vs_currencies=usd',
            headers={'User-Agent': 'LightWeather/1.0', 'Accept': 'application/json'}
        )
        with _urllib_req.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            price = (data.get('lightchain-ai') or {}).get('usd')
            if price and float(price) > 0:
                _lcai_price_cache = {'price': float(price), 'ts': now}
                return float(price)
    except Exception:
        pass
    # Try DexScreener
    try:
        req = _urllib_req.Request(
            'https://api.dexscreener.com/latest/dex/search?q=LCAI',
            headers={'User-Agent': 'LightWeather/1.0', 'Accept': 'application/json'}
        )
        with _urllib_req.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            for pair in (data.get('pairs') or []):
                price = float(pair.get('priceUsd') or 0)
                if price > 0:
                    _lcai_price_cache = {'price': price, 'ts': now}
                    return price
    except Exception:
        pass
    _lcai_price_cache['ts'] = now
    return _lcai_price_cache['price']

def lightchain_rpc(method, params):
    payload = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1}).encode()
    req = _urllib_req.Request(
        'https://node1.lightchain.ai', data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'LightWeather/1.0'}
    )
    with _urllib_req.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

# ── PREMIUM ENDPOINTS ────────────────────────────────────────────────────────

@app.route('/api/lw/price')
def api_lw_price():
    price = get_lcai_price()
    required_lcai = MONTHLY_PRICE_USD / price
    return jsonify({
        'usd': MONTHLY_PRICE_USD,
        'lcai_price_usd': price,
        'required_lcai': round(required_lcai, 2),
        'owner_wallet': OWNER_WALLET
    })

@app.route('/api/lw/subscription/<wallet>')
def api_lw_subscription(wallet):
    w = wallet.lower().strip()
    if w in PREMIUM_WHITELIST:
        return jsonify({'subscribed': True, 'expires_at': None, 'whitelisted': True})
    now = int(time.time())
    conn = get_db()
    row = conn.execute('SELECT expires_at FROM subscriptions WHERE wallet = ?', (w,)).fetchone()
    conn.close()
    subscribed = bool(row and row['expires_at'] and row['expires_at'] > now)
    return jsonify({'subscribed': subscribed, 'expires_at': row['expires_at'] if row else None})

@app.route('/api/lw/verify-subscription', methods=['POST'])
def api_lw_verify_subscription():
    data     = request.json or {}
    w        = (data.get('wallet') or '').lower().strip()
    tx_hash  = (data.get('tx_hash') or '').strip()
    if not w or not tx_hash:
        return jsonify({'error': 'wallet and tx_hash required'}), 400
    if w in PREMIUM_WHITELIST:
        return jsonify({'success': True, 'subscribed': True, 'whitelisted': True})
    try:
        result = lightchain_rpc('eth_getTransactionByHash', [tx_hash])
        tx = result.get('result')
        if not tx:
            return jsonify({'error': 'Transaction not found on Lightchain. Check the hash and try again.'}), 404
        to_addr = (tx.get('to') or '').lower()
        if to_addr != OWNER_WALLET:
            return jsonify({'error': 'This transaction was not sent to the LightWeather subscription address.'}), 400
        # Check amount
        price         = get_lcai_price()
        required_lcai = MONTHLY_PRICE_USD / price
        required_wei  = int(required_lcai * 1e18 * 0.95)   # 5% tolerance
        tx_value      = int(tx.get('value', '0x0'), 16)
        if tx_value < required_wei:
            sent   = round(tx_value / 1e18, 4)
            needed = round(required_lcai, 2)
            return jsonify({'error': f'Insufficient amount — sent {sent} LCAI, needed ~{needed} LCAI'}), 400
        # Check receipt
        try:
            rcpt = lightchain_rpc('eth_getTransactionReceipt', [tx_hash]).get('result')
            if rcpt and rcpt.get('status') == '0x0':
                return jsonify({'error': 'Transaction was reverted. Please send a new one.'}), 400
        except Exception:
            pass
        # Store 30-day subscription
        expires_at = int(time.time()) + 30 * 24 * 60 * 60
        conn = get_db()
        conn.execute(
            'INSERT OR REPLACE INTO subscriptions (wallet, expires_at, tx_hash) VALUES (?, ?, ?)',
            (w, expires_at, tx_hash)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'expires_at': expires_at})
    except Exception as e:
        return jsonify({'error': 'Could not verify transaction: ' + str(e)}), 500

# ── AIVM CONFIG ──────────────────────────────────────────────────────────────
AIVM_PRIVATE_KEY = os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip()
AIVM_GATEWAY     = "https://chat-api.mainnet.lightchain.ai"
AIVM_RELAY       = "wss://relay.mainnet.lightchain.ai/ws"
AIVM_JOB_REG     = "0xfB15F90298e4CcD7106E76fFB5e520315cC42B0b"
AIVM_JOB_FEE     = 20_000_000_000_000_000
AIVM_CHAIN_ID    = 9200

AIVM_ABI = [
    {"name":"createSession","type":"function","stateMutability":"payable",
     "inputs":[{"name":"paramsHash","type":"bytes32"},{"name":"worker","type":"address"},
               {"name":"encWorkerKey","type":"bytes"},{"name":"ephemeralPubKey","type":"bytes"},
               {"name":"initState","type":"bytes"},{"name":"expiry","type":"uint256"}],
     "outputs":[{"name":"sessionId","type":"uint256"}]},
    {"name":"submitJob","type":"function","stateMutability":"payable",
     "inputs":[{"name":"sessionId","type":"uint256"},{"name":"promptHash","type":"bytes32"}],
     "outputs":[{"name":"jobId","type":"uint256"}]},
    {"anonymous":False,"name":"SessionCreated","type":"event",
     "inputs":[{"indexed":True,"name":"sessionId","type":"uint256"},
               {"indexed":True,"name":"user","type":"address"},
               {"indexed":True,"name":"paramsHash","type":"bytes32"},
               {"indexed":False,"name":"worker","type":"address"},
               {"indexed":False,"name":"encWorkerKey","type":"bytes"},
               {"indexed":False,"name":"ephemeralPubKey","type":"bytes"}]},
    {"anonymous":False,"name":"JobCompleted","type":"event",
     "inputs":[{"indexed":True,"name":"jobId","type":"uint256"},
               {"indexed":True,"name":"worker","type":"address"},
               {"indexed":False,"name":"responseHash","type":"bytes32"},
               {"indexed":False,"name":"ciphertextHash","type":"bytes32"}]},
]

def _aivm_decode_pubkey(s):
    if isinstance(s, (bytes, bytearray)): return bytes(s)
    s = s.strip()
    if s.startswith('0x') or s.startswith('0X'): b = bytes.fromhex(s[2:])
    elif len(s) == 130 and all(c in '0123456789abcdefABCDEF' for c in s): b = bytes.fromhex(s)
    else: b = _b64_mod.b64decode(s)
    if len(b) != 65: raise ValueError(f"pubkey: expected 65 bytes, got {len(b)}")
    return b

def _aivm_ecdh_wrap(session_key, peer_pub_bytes):
    from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, ECDH, EllipticCurvePublicNumbers, SECP256R1
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.backends import default_backend
    x = int.from_bytes(peer_pub_bytes[1:33], 'big')
    y = int.from_bytes(peer_pub_bytes[33:65], 'big')
    peer_pub   = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key(default_backend())
    ephem_priv = generate_private_key(SECP256R1(), default_backend())
    shared     = ephem_priv.exchange(ECDH(), peer_pub)
    pub_nums   = ephem_priv.public_key().public_numbers()
    ephem_pub_bytes = b'\x04' + pub_nums.x.to_bytes(32, 'big') + pub_nums.y.to_bytes(32, 'big')
    nonce  = _secrets_mod.token_bytes(12)
    ct_tag = AESGCM(shared).encrypt(nonce, session_key, None)
    return ephem_pub_bytes + nonce + ct_tag

def _aivm_aes_encrypt(key, plaintext):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = _secrets_mod.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)

def _aivm_aes_decrypt(key, blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 28: raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)

class AIVMClient:
    def __init__(self, private_key):
        from web3 import Web3
        from eth_account import Account
        self._req     = _requests
        self._w3      = Web3(Web3.HTTPProvider(LCAI_RPC))
        self._account = Account.from_key(private_key)
        self._registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(AIVM_JOB_REG), abi=AIVM_ABI)
        self._jwt = None; self._jwt_exp = 0

    def _get_jwt(self):
        from eth_account.messages import encode_defunct
        if self._jwt and time.time() < self._jwt_exp - 30: return self._jwt
        r = self._req.get(f"{AIVM_GATEWAY}/api/auth/challenge",
                          params={"address": self._account.address}, timeout=15)
        r.raise_for_status()
        resp_json = r.json()
        message = resp_json.get("message") or resp_json.get("nonce") or list(resp_json.values())[0]
        sig = self._account.sign_message(encode_defunct(text=message))
        r2 = self._req.post(f"{AIVM_GATEWAY}/api/auth/verify",
                            json={"message": message, "signature": "0x" + sig.signature.hex()}, timeout=15)
        r2.raise_for_status()
        v = r2.json(); self._jwt = v["token"]
        exp_str = v["expiresAt"][:19].replace("T", " ")
        self._jwt_exp = time.mktime(time.strptime(exp_str, "%Y-%m-%d %H:%M:%S"))
        return self._jwt

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self._get_jwt()}", "Accept": "application/json", "Content-Type": "application/json"}

    def run_inference(self, prompt, timeout_secs=240):
        import websocket as _ws
        from web3 import Web3
        from urllib.parse import quote as _url_quote
        req = self._req
        r = req.get(f"{AIVM_GATEWAY}/api/models", timeout=15); r.raise_for_status()
        models = r.json().get("models", [])
        model  = next((m for m in models if m["name"] == "llama3-8b"), models[0] if models else None)
        if not model: raise RuntimeError("No AIVM models available")
        model_id = model["id"]
        r = req.post(f"{AIVM_GATEWAY}/api/sessions/select",
                     json={"modelId": model_id}, headers=self._auth_headers(), timeout=15)
        r.raise_for_status(); sel = r.json()
        session_key  = _secrets_mod.token_bytes(32)
        enc_worker   = _aivm_ecdh_wrap(session_key, _aivm_decode_pubkey(sel["workerEncryptionKey"]))
        enc_disputer = _aivm_ecdh_wrap(session_key, _aivm_decode_pubkey(sel["disputerEncryptionKey"]))
        r = req.post(f"{AIVM_GATEWAY}/api/sessions/prepare",
                     json={"modelId": model_id,
                           "encWorkerKey":   _b64_mod.b64encode(enc_worker).decode(),
                           "encDisputerKey": _b64_mod.b64encode(enc_disputer).decode()},
                     headers=self._auth_headers(), timeout=15)
        r.raise_for_status(); prep = r.json()
        params_hash = bytes.fromhex(model_id[2:].zfill(64) if model_id[:2].lower() == "0x" else model_id.zfill(64))
        sig_bytes   = bytes.fromhex(prep["signature"][2:] if prep["signature"][:2].lower() == "0x" else prep["signature"])
        gas_price   = self._w3.eth.gas_price
        nonce_val   = self._w3.eth.get_transaction_count(self._account.address)
        tx = self._registry.functions.createSession(
            params_hash, Web3.to_checksum_address(prep["worker"]),
            enc_worker, enc_disputer, sig_bytes, prep["expiry"]
        ).build_transaction({"from": self._account.address, "nonce": nonce_val,
                              "gas": 1_000_000, "gasPrice": gas_price, "value": 0, "chainId": AIVM_CHAIN_ID})
        signed  = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt1 = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt1.status != 1: raise RuntimeError("createSession reverted")
        session_id = None
        for log in receipt1.logs:
            try:
                evt = self._registry.events.SessionCreated().process_log(log)
                session_id = evt["args"]["sessionId"]; break
            except Exception: pass
        if session_id is None: raise RuntimeError("SessionCreated event not found")
        relay_token = None
        deadline = time.time() + 60
        while time.time() < deadline:
            r = req.get(f"{AIVM_GATEWAY}/api/sessions/{session_id}/token",
                        headers=self._auth_headers(), timeout=10)
            if r.status_code == 200 and r.json().get("token"):
                relay_token = r.json()["token"]; break
            time.sleep(1)
        if not relay_token: raise RuntimeError("Relay token not ready")
        chunks = []; ws_ready = threading.Event(); ws_err = [None]
        def _on_message(ws_obj, msg):
            try:
                frame = json.loads(msg); payload = frame.get("payload")
                if payload:
                    blob = _b64_mod.b64decode(payload)
                    pt   = _aivm_aes_decrypt(session_key, blob)
                    chunks.append(pt.decode("utf-8", errors="replace"))
            except Exception: pass
        def _on_open(ws_obj): ws_ready.set()
        def _on_error(ws_obj, err): ws_err[0] = err; ws_ready.set()
        ws = _ws.WebSocketApp(f"{AIVM_RELAY}?token={_url_quote(relay_token)}",
                              on_message=_on_message, on_open=_on_open, on_error=_on_error)
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True); ws_thread.start()
        ws_ready.wait(timeout=15)
        if ws_err[0]: raise RuntimeError(f"WebSocket failed: {ws_err[0]}")
        cipher = _aivm_aes_encrypt(session_key, prompt.encode("utf-8"))
        r = req.post(f"{AIVM_GATEWAY}/api/blobs",
                     json={"data": _b64_mod.b64encode(cipher).decode()},
                     headers=self._auth_headers(), timeout=15)
        r.raise_for_status()
        blob_hashes = r.json().get("blobHashes", [])
        if not blob_hashes: raise RuntimeError("No blob hash")
        _bh = blob_hashes[0]
        prompt_hash = bytes.fromhex(_bh[2:].zfill(64) if _bh[:2].lower() == "0x" else _bh.zfill(64))
        nonce_val2 = self._w3.eth.get_transaction_count(self._account.address)
        tx2 = self._registry.functions.submitJob(session_id, prompt_hash).build_transaction({
            "from": self._account.address, "nonce": nonce_val2,
            "gas": 500_000, "gasPrice": gas_price, "value": AIVM_JOB_FEE, "chainId": AIVM_CHAIN_ID})
        signed2  = self._account.sign_transaction(tx2)
        tx_hash2 = self._w3.eth.send_raw_transaction(signed2.raw_transaction)
        receipt2 = self._w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=90)
        if receipt2.status != 1: raise RuntimeError("submitJob reverted — check LCAI balance")
        job_completed_topic = "0x" + Web3.keccak(text="JobCompleted(uint256,address,bytes32,bytes32)").hex()
        done = False; deadline = time.time() + timeout_secs
        while time.time() < deadline and not done:
            time.sleep(5)
            if chunks: done = True; break
            try:
                logs = self._w3.eth.get_logs({"address": Web3.to_checksum_address(AIVM_JOB_REG),
                                               "fromBlock": receipt2.blockNumber,
                                               "toBlock": self._w3.eth.block_number,
                                               "topics": [job_completed_topic]})
                if logs: done = True
            except Exception: pass
        time.sleep(3); ws.close()
        result = "".join(chunks).strip()
        if not result and not done: raise RuntimeError(f"Timeout after {timeout_secs}s")
        return result or "No response from AIVM worker"

_aivm_client = None
_aivm_lock   = threading.Lock()

def get_aivm():
    global _aivm_client
    with _aivm_lock:
        if _aivm_client is None and AIVM_PRIVATE_KEY:
            _aivm_client = AIVMClient(AIVM_PRIVATE_KEY)
        return _aivm_client

# ── OPEN-METEO FALLBACK ───────────────────────────────────────────────────────

WMO_TEXT = {
    0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Depositing rime fog', 51: 'Light drizzle', 53: 'Drizzle',
    55: 'Heavy drizzle', 56: 'Freezing drizzle', 57: 'Heavy freezing drizzle',
    61: 'Slight rain', 63: 'Moderate rain', 65: 'Heavy rain',
    66: 'Light freezing rain', 67: 'Heavy freezing rain',
    71: 'Slight snow', 73: 'Moderate snow', 75: 'Heavy snow', 77: 'Snow grains',
    80: 'Rain showers', 81: 'Moderate rain showers', 82: 'Violent rain showers',
    85: 'Snow showers', 86: 'Heavy snow showers',
    95: 'Thunderstorm', 96: 'Thunderstorm with hail', 99: 'Thunderstorm with heavy hail',
}

def _wmo_text(code):
    return WMO_TEXT.get(int(code or 0), 'Unknown')

def _parse_coords(q):
    if ',' not in q:
        return None
    parts = [p.strip() for p in q.split(',', 1)]
    try:
        lat = float(parts[0])
        lon = float(parts[1].split(',')[0].strip())
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except (TypeError, ValueError):
        pass
    return None

def _geocode_open_meteo(q):
    coords = _parse_coords(q)
    if coords:
        return {'latitude': coords[0], 'longitude': coords[1],
                'name': f"{coords[0]:.2f}, {coords[1]:.2f}", 'admin1': '', 'country': ''}
    r = _requests.get('https://geocoding-api.open-meteo.com/v1/search',
                      params={'name': q, 'count': 1, 'language': 'en', 'format': 'json'}, timeout=8)
    r.raise_for_status()
    results = (r.json() or {}).get('results') or []
    if not results:
        return None
    hit = results[0]
    return {
        'latitude': hit['latitude'], 'longitude': hit['longitude'],
        'name': hit.get('name') or q,
        'admin1': hit.get('admin1') or '',
        'country': hit.get('country') or '',
    }

def _open_meteo_forecast(lat, lon):
    r = _requests.get('https://api.open-meteo.com/v1/forecast', params={
        'latitude': lat, 'longitude': lon, 'timezone': 'auto', 'forecast_days': 7,
        'current': ','.join([
            'temperature_2m', 'apparent_temperature', 'relative_humidity_2m',
            'weather_code', 'wind_speed_10m', 'wind_direction_10m',
            'surface_pressure', 'uv_index'
        ]),
        'daily': ','.join([
            'weather_code', 'temperature_2m_max', 'temperature_2m_min',
            'precipitation_probability_max', 'sunrise', 'sunset', 'uv_index_max'
        ]),
    }, timeout=12)
    r.raise_for_status()
    return r.json()

def _wind_dir_label(deg):
    dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    try:
        idx = int((float(deg) + 11.25) / 22.5) % 16
        return dirs[idx]
    except (TypeError, ValueError):
        return ''

def _open_meteo_to_weatherapi(geo, om):
    cur = om.get('current') or {}
    daily = om.get('daily') or {}
    tz = om.get('timezone') or 'UTC'
    code = int(cur.get('weather_code') or 0)
    text = _wmo_text(code)
    forecastday = []
    dates = daily.get('time') or []
    for i, date in enumerate(dates):
        day_code = int((daily.get('weather_code') or [0])[i] or 0)
        sunrise = ((daily.get('sunrise') or [''])[i] or '').split('T')[-1]
        sunset  = ((daily.get('sunset') or [''])[i] or '').split('T')[-1]
        forecastday.append({
            'date': date,
            'day': {
                'maxtemp_c': (daily.get('temperature_2m_max') or [0])[i],
                'mintemp_c': (daily.get('temperature_2m_min') or [0])[i],
                'daily_chance_of_rain': (daily.get('precipitation_probability_max') or [0])[i] or 0,
                'condition': {'text': _wmo_text(day_code), 'icon': ''},
            },
            'astro': {'sunrise': sunrise, 'sunset': sunset},
        })
    localtime = time.strftime('%Y-%m-%d %I:%M %p', time.localtime())
    return {
        'location': {
            'name': geo.get('name') or 'Unknown',
            'region': geo.get('admin1') or '',
            'country': geo.get('country') or '',
            'localtime': localtime,
            'tz_id': tz,
        },
        'current': {
            'temp_c': cur.get('temperature_2m'),
            'feelslike_c': cur.get('apparent_temperature'),
            'humidity': cur.get('relative_humidity_2m'),
            'wind_kph': cur.get('wind_speed_10m'),
            'wind_dir': _wind_dir_label(cur.get('wind_direction_10m')),
            'vis_km': 10,
            'pressure_mb': cur.get('surface_pressure'),
            'uv': cur.get('uv_index') or 0,
            'condition': {'text': text, 'icon': ''},
        },
        'forecast': {'forecastday': forecastday},
        'alerts': {'alert': []},
        '_source': 'open-meteo',
    }

def _weather_from_open_meteo(q):
    geo = _geocode_open_meteo(q)
    if not geo:
        return None
    om = _open_meteo_forecast(geo['latitude'], geo['longitude'])
    return _open_meteo_to_weatherapi(geo, om)

def _search_open_meteo(q):
    r = _requests.get('https://geocoding-api.open-meteo.com/v1/search',
                      params={'name': q, 'count': 6, 'language': 'en', 'format': 'json'}, timeout=8)
    r.raise_for_status()
    return [{
        'name': hit.get('name') or '',
        'region': hit.get('admin1') or '',
        'country': hit.get('country') or '',
    } for hit in ((r.json() or {}).get('results') or [])]

# ── WEATHER ENDPOINTS ─────────────────────────────────────────────────────────

@app.route('/api/weather')
def get_weather():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'q required'}), 400
    if WEATHERAPI_KEY:
        try:
            r = _requests.get(f"{WEATHERAPI_BASE}/forecast.json",
                              params={'key': WEATHERAPI_KEY, 'q': q, 'days': 7, 'alerts': 'yes', 'aqi': 'no'},
                              timeout=10)
            r.raise_for_status()
            return jsonify(r.json())
        except Exception as e:
            print(f"[weather] WeatherAPI failed for {q!r}: {e}")
    try:
        payload = _weather_from_open_meteo(q)
        if payload:
            return jsonify(payload)
    except Exception as e:
        print(f"[weather] Open-Meteo failed for {q!r}: {e}")
    return jsonify({'error': 'Location not found'}), 404

@app.route('/api/search')
def search_cities():
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify([])
    if WEATHERAPI_KEY:
        try:
            r = _requests.get(f"{WEATHERAPI_BASE}/search.json",
                              params={'key': WEATHERAPI_KEY, 'q': q}, timeout=5)
            r.raise_for_status()
            data = r.json()
            if data:
                return jsonify(data)
        except Exception as e:
            print(f"[search] WeatherAPI failed for {q!r}: {e}")
    try:
        return jsonify(_search_open_meteo(q))
    except Exception as e:
        print(f"[search] Open-Meteo failed for {q!r}: {e}")
        return jsonify([])

@app.route('/api/explain', methods=['POST'])
def explain_weather():
    data      = request.json or {}
    summary   = data.get('summary', '')
    language  = data.get('language', 'en')
    chat_mode = data.get('chat_mode', False)
    if not summary:
        return jsonify({'error': 'summary required'}), 400

    # Rate-limit paid AIVM (subscription does not unlock unlimited abuse)
    ok, code, err = _gate_ai()
    if not ok:
        return jsonify({'error': err, 'explanation': _fallback_explanation(summary, language)}), code
    try:
        return _explain_weather_inner(summary, language, chat_mode)
    finally:
        _ungate_ai()

def _explain_weather_inner(summary, language, chat_mode):
    if chat_mode:
        # In chat mode the frontend sends the full ready-to-go prompt
        prompt = summary
    else:
        lang_names = {
            'en': 'English', 'zh': 'Mandarin Chinese', 'es': 'Spanish',
            'fr': 'French', 'ja': 'Japanese', 'ko': 'Korean'
        }
        lang_name = lang_names.get(language, 'English')
        prompt = (
            f"You are a friendly, helpful weather assistant. Based on this weather data, write a "
            f"natural 2-3 sentence summary of what to expect — be practical and specific about "
            f"what to wear or bring. Mention any alerts if present. Be warm and conversational.\n\n"
            f"{summary}\n\n"
            f"Respond in {lang_name} only. Keep it under 80 words."
        )

    client = get_aivm()
    if not client:
        return jsonify({'explanation': _fallback_explanation(summary, language)})

    try:
        result = client.run_inference(prompt, timeout_secs=180)
        return jsonify({'explanation': result})
    except Exception as e:
        print(f"[AIVM] error: {e}")
        return jsonify({'explanation': _fallback_explanation(summary, language)})

def _fallback_explanation(summary, language):
    """Simple rule-based fallback if AIVM is unavailable."""
    fallbacks = {
        'en': "Weather data loaded. Tap the AI button again in a moment — the AI assistant is warming up.",
        'zh': "天气数据已加载。请稍后再次点击AI按钮。",
        'es': "Datos meteorológicos cargados. Toque el botón de IA en un momento.",
        'fr': "Données météo chargées. Appuyez à nouveau sur le bouton IA dans un moment.",
        'ja': "気象データが読み込まれました。しばらくしてからAIボタンをもう一度タップしてください。",
        'ko': "날씨 데이터가 로드되었습니다. 잠시 후 AI 버튼을 다시 탭하세요.",
    }
    return fallbacks.get(language, fallbacks['en'])

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'time': int(time.time())})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
