#!/usr/bin/env python3
"""LightWeather — Backend server. Weather data + AIVM forecast explanation."""

import os, time, json, threading, base64 as _b64_mod, secrets as _secrets_mod
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as _requests

app = Flask(__name__)
CORS(app, origins="*")

WEATHERAPI_KEY = os.environ.get("WEATHERAPI_KEY", "")
WEATHERAPI_BASE = "https://api.weatherapi.com/v1"
LCAI_RPC = "https://rpc.mainnet.lightchain.ai"

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

# ── WEATHER ENDPOINTS ─────────────────────────────────────────────────────────

@app.route('/api/weather')
def get_weather():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'q required'}), 400
    if not WEATHERAPI_KEY:
        return jsonify({'error': 'Weather API not configured'}), 500
    try:
        r = _requests.get(f"{WEATHERAPI_BASE}/forecast.json",
                          params={'key': WEATHERAPI_KEY, 'q': q, 'days': 7, 'alerts': 'yes', 'aqi': 'no'},
                          timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except _requests.exceptions.HTTPError as e:
        return jsonify({'error': 'Location not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/search')
def search_cities():
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify([])
    try:
        r = _requests.get(f"{WEATHERAPI_BASE}/search.json",
                          params={'key': WEATHERAPI_KEY, 'q': q}, timeout=5)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception:
        return jsonify([])

@app.route('/api/explain', methods=['POST'])
def explain_weather():
    data     = request.json or {}
    summary  = data.get('summary', '')
    language = data.get('language', 'en')
    if not summary:
        return jsonify({'error': 'summary required'}), 400

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
        # Fallback: rule-based summary if no AIVM key
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
