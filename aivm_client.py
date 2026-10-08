"""Direct Lightchain AIVM client — SORTITION flow (matches the live SCE client).

Dispatcher-free "sortition" session model: the worker self-selects on-chain and
the gateway opens the session from the service's prepaid JobRegistry balance.
Replaces the retired classic /api/sessions/select path (which now 401s network-wide).

Each inference reports which worker served it (self.last_worker) so the jury can
draw 5 DISTINCT jurors (dedupe by worker, redraw duplicates). Calls are serialized
by a module-level tx lock, so jurors run one after another — never 5 at once.

Private key stays in env (LIGHTCHAIN_PRIVATE_KEY) — never log it.
"""

import base64
import json
import os
import secrets
import threading
import time
from urllib.parse import quote as url_quote

AIVM_GATEWAY = "https://chat-api.mainnet.lightchain.ai"
AIVM_RELAY = "wss://relay.mainnet.lightchain.ai/ws"
AIVM_RPC = "https://rpc.mainnet.lightchain.ai"
AIVM_JOB_REG = "0xfB15F90298e4CcD7106E76fFB5e520315cC42B0b"
AIVM_JOB_FEE = 20_000_000_000_000_000       # 0.02 LCAI — one job
AIVM_PREPAY = 400_000_000_000_000_000       # 0.4 LCAI prepaid — ~20 jobs, not a drain
AIVM_GAS_RESERVE = 50_000_000_000_000_000   # 0.05 LCAI left in the wallet
AIVM_CHAIN_ID = 9200
AIVM_DRAW_TIMEOUT = 90                       # sortition draw waits on live workers
AIVM_MODEL = os.environ.get("AIVM_MODEL_PRIMARY", "llama3-8b").strip() or "llama3-8b"

AIVM_ABI = [
    {
        "name": "deposit", "type": "function", "stateMutability": "payable",
        "inputs": [],
        "outputs": [],
    },
    {
        "name": "depositAndAuthorize", "type": "function", "stateMutability": "payable",
        "inputs": [{"name": "delegate", "type": "address"}],
        "outputs": [],
    },
    {
        "name": "setDelegateAuthorization", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "delegate", "type": "address"},
            {"name": "authorized", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "createSession", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "paramsHash", "type": "bytes32"},
            {"name": "worker", "type": "address"},
            {"name": "encWorkerKey", "type": "bytes"},
            {"name": "ephemeralPubKey", "type": "bytes"},
            {"name": "initState", "type": "bytes"},
            {"name": "expiry", "type": "uint256"},
        ],
        "outputs": [{"name": "sessionId", "type": "uint256"}],
    },
    {
        "name": "submitJob", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "sessionId", "type": "uint256"},
            {"name": "promptHash", "type": "bytes32"},
        ],
        "outputs": [{"name": "jobId", "type": "uint256"}],
    },
    {
        "anonymous": False, "name": "SessionCreated", "type": "event",
        "inputs": [
            {"indexed": True, "name": "sessionId", "type": "uint256"},
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": True, "name": "paramsHash", "type": "bytes32"},
            {"indexed": False, "name": "worker", "type": "address"},
            {"indexed": False, "name": "encWorkerKey", "type": "bytes"},
            {"indexed": False, "name": "ephemeralPubKey", "type": "bytes"},
        ],
    },
    {
        "anonymous": False, "name": "JobSubmitted", "type": "event",
        "inputs": [
            {"indexed": True, "name": "jobId", "type": "uint256"},
            {"indexed": True, "name": "sessionId", "type": "uint256"},
            {"indexed": False, "name": "worker", "type": "address"},
        ],
    },
    {
        "anonymous": False, "name": "JobCompleted", "type": "event",
        "inputs": [
            {"indexed": True, "name": "jobId", "type": "uint256"},
            {"indexed": True, "name": "worker", "type": "address"},
            {"indexed": False, "name": "responseHash", "type": "bytes32"},
            {"indexed": False, "name": "ciphertextHash", "type": "bytes32"},
        ],
    },
]


def _decode_pubkey(s):
    """Accept hex (with/without 0x) or base64; return 65-byte uncompressed P-256 point."""
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    s = s.strip()
    if s.startswith("0x") or s.startswith("0X"):
        b = bytes.fromhex(s[2:])
    elif len(s) == 130 and all(c in "0123456789abcdefABCDEF" for c in s):
        b = bytes.fromhex(s)
    else:
        b = base64.b64decode(s)
    if len(b) != 65:
        raise ValueError(f"pubkey decode: expected 65 bytes, got {len(b)}")
    return b


def _ecdh_wrap(session_key, peer_pub_bytes):
    """ECDH-wrap session_key for peer P-256 pubkey. Raw ECDH X-coord = AES-256 key."""
    from cryptography.hazmat.primitives.asymmetric.ec import (
        generate_private_key, ECDH, EllipticCurvePublicNumbers, SECP256R1,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.backends import default_backend

    x = int.from_bytes(peer_pub_bytes[1:33], "big")
    y = int.from_bytes(peer_pub_bytes[33:65], "big")
    peer_pub = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key(default_backend())
    ephem_priv = generate_private_key(SECP256R1(), default_backend())
    shared = ephem_priv.exchange(ECDH(), peer_pub)
    pub_nums = ephem_priv.public_key().public_numbers()
    ephem_pub_bytes = (
        b"\x04" + pub_nums.x.to_bytes(32, "big") + pub_nums.y.to_bytes(32, "big")
    )
    nonce = secrets.token_bytes(12)
    ct_tag = AESGCM(shared).encrypt(nonce, session_key, None)
    return ephem_pub_bytes + nonce + ct_tag


def _aes_encrypt(key, plaintext):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _aes_decrypt(key, blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 28:
        raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


def _hex0x(b):
    return "0x" + bytes(b).hex()


def _parse_jwt_exp(expires_at):
    """Parse consumer-api expiresAt as UTC seconds. Naive timestamps treated as UTC."""
    from datetime import datetime, timezone
    s = (expires_at or "").strip()
    if not s:
        return 0.0
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# Serialize on-chain AIVM txs so concurrent jobs don't collide on nonce
_aivm_tx_lock = threading.Lock()


class AIVMClient:
    """LLM inference through Lightchain consumer-api sortition.

    Prepaid JobRegistry balance + authorised delegate; the gateway opens the
    session on chain. Requires a funded Lightchain mainnet wallet.
    """

    def __init__(self, private_key):
        import requests as _req
        from web3 import Web3
        from eth_account import Account

        self._req = _req
        self._w3 = Web3(Web3.HTTPProvider(AIVM_RPC))
        self._account = Account.from_key(private_key)
        self._registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(AIVM_JOB_REG),
            abi=AIVM_ABI,
        )
        self._jwt = None
        self._jwt_exp = 0
        self._local_nonce = None
        self.last_worker = None  # worker address that served the last inference
        print(f"  [AIVM] wallet: {self._account.address}")

    def _next_nonce(self):
        pending = self._w3.eth.get_transaction_count(self._account.address, "pending")
        latest = self._w3.eth.get_transaction_count(self._account.address, "latest")
        base = max(pending, latest)
        if self._local_nonce is None or self._local_nonce < base:
            self._local_nonce = base
        n = self._local_nonce
        self._local_nonce = n + 1
        return n

    def _gas_price(self, bump=1.25):
        gp = int(self._w3.eth.gas_price or 0)
        if gp <= 0:
            gp = 1_000_000_000
        return max(int(gp * bump), gp + 1)

    def _send_contract_tx(self, built_fn, *, gas, value=0, label="tx"):
        last_err = None
        gas_mult = 1.35
        for attempt in range(5):
            try:
                nonce = self._next_nonce()
                gas_price = self._gas_price(gas_mult)
                tx = built_fn.build_transaction({
                    "from": self._account.address,
                    "nonce": nonce,
                    "gas": gas,
                    "gasPrice": gas_price,
                    "value": value,
                    "chainId": AIVM_CHAIN_ID,
                })
                signed = self._account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  [AIVM] {label} tx nonce={nonce} attempt={attempt + 1}")
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt.status != 1:
                    raise RuntimeError(f"{label} reverted on-chain")
                return receipt
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                retryable = any(s in msg for s in (
                    "underpriced", "replacement transaction", "nonce too low",
                    "already known", "alreadyimported",
                ))
                print(f"  [AIVM] {label} send failed attempt {attempt + 1}: {e}")
                if not retryable or attempt == 4:
                    break
                self._local_nonce = None
                gas_mult *= 1.4
                time.sleep(1.2 + attempt * 0.5)
        raise RuntimeError(f"AIVM {label} failed after retries: {last_err}")

    def _get_jwt(self):
        from eth_account.messages import encode_defunct
        if self._jwt and time.time() < self._jwt_exp - 30:
            return self._jwt
        r = self._req.get(
            f"{AIVM_GATEWAY}/api/auth/challenge",
            params={"address": self._account.address}, timeout=15,
        )
        r.raise_for_status()
        message = r.json()["message"]
        sig = self._account.sign_message(encode_defunct(text=message))
        r2 = self._req.post(
            f"{AIVM_GATEWAY}/api/auth/verify",
            json={"message": message, "signature": "0x" + sig.signature.hex()},
            timeout=15,
        )
        r2.raise_for_status()
        v = r2.json()
        self._jwt = v["token"]
        self._jwt_exp = _parse_jwt_exp(v.get("expiresAt") or "")
        if not self._jwt_exp:
            self._jwt_exp = time.time() + 3300
        return self._jwt

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self._get_jwt()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _api_error(self, r, label):
        try:
            d = r.json()
            return str(d.get("message") or d.get("error") or r.text[:240])
        except Exception:
            return (r.text or "")[:240] or f"HTTP {r.status_code}"

    def _api_json(self, method, path, *, json_body=None, timeout=20, ok=None):
        url = f"{AIVM_GATEWAY}{path}"
        r = self._req.request(
            method, url, json=json_body, headers=self._auth_headers(), timeout=timeout,
        )
        allowed = range(200, 300) if ok is None else ok
        if r.status_code not in allowed:
            raise RuntimeError(
                f"AIVM {path} HTTP {r.status_code}: {self._api_error(r, path)[:180]}")
        if not r.content:
            return {}
        try:
            return r.json()
        except Exception:
            return {}

    def _ensure_prepaid(self):
        """Sortition jobs are paid from JobRegistry prepaid by an authorised
        delegate. Top up modestly if needed; never drain the service wallet."""
        from web3 import Web3

        bal = self._api_json("GET", "/api/balance", timeout=15)
        prepaid = int(bal.get("balance") or 0)
        delegate = (bal.get("delegate") or "").strip()
        authorized = bal.get("delegateAuthorized") is True
        print(
            f"  [AIVM] prepaid={prepaid / 1e18:.4f} LCAI "
            f"delegateAuthorized={authorized} "
            f"delegate={delegate[:10] + '…' if delegate else 'none'}",
            flush=True,
        )
        if authorized and prepaid >= AIVM_JOB_FEE:
            return

        if not delegate or not delegate.startswith("0x") or len(delegate) != 42:
            raise RuntimeError("AIVM /api/balance did not return a delegate address")

        native = int(self._w3.eth.get_balance(self._account.address))
        want = AIVM_PREPAY
        if prepaid > 0 and prepaid < AIVM_JOB_FEE:
            want = max(AIVM_PREPAY - prepaid, AIVM_JOB_FEE)
        affordable = native - AIVM_GAS_RESERVE
        if affordable < AIVM_JOB_FEE:
            raise RuntimeError(
                f"AIVM wallet too low to prepay without draining "
                f"(native={native / 1e18:.4f} LCAI, "
                f"need ≥{(AIVM_JOB_FEE + AIVM_GAS_RESERVE) / 1e18:.3f})"
            )
        amount = min(min(want, affordable), AIVM_PREPAY)
        print(
            f"  [AIVM] topping up prepaid {amount / 1e18:.4f} LCAI "
            f"(wallet native {native / 1e18:.4f} LCAI; leaving ≥{AIVM_GAS_RESERVE / 1e18:.3f})"
        )

        delegate_cs = Web3.to_checksum_address(delegate)
        if authorized:
            self._send_contract_tx(
                self._registry.functions.deposit(),
                gas=200_000, value=amount, label="deposit",
            )
        else:
            self._send_contract_tx(
                self._registry.functions.depositAndAuthorize(delegate_cs),
                gas=250_000, value=amount, label="depositAndAuthorize",
            )

        for _ in range(8):
            time.sleep(2)
            bal = self._api_json("GET", "/api/balance", timeout=15)
            prepaid = int(bal.get("balance") or 0)
            authorized = bal.get("delegateAuthorized") is True
            if authorized and prepaid >= AIVM_JOB_FEE:
                print(f"  [AIVM] prepaid ready {prepaid / 1e18:.4f} LCAI")
                return
        raise RuntimeError(
            f"AIVM prepaid not visible after deposit "
            f"(balance={prepaid}, delegateAuthorized={authorized})"
        )

    def run_inference(self, prompt, timeout_secs=360):
        with _aivm_tx_lock:
            self._local_nonce = None
            self.last_worker = None
            return self._run_inference_locked(prompt, timeout_secs)

    def _run_inference_locked(self, prompt, timeout_secs=360):
        import websocket as _ws

        req = self._req
        print(f"  [AIVM] starting sortition inference ({len(prompt)} chars)", flush=True)

        r = req.get(f"{AIVM_GATEWAY}/api/models", timeout=15)
        r.raise_for_status()
        models = r.json().get("models", [])
        model = next((m for m in models if m.get("name") == AIVM_MODEL),
                     models[0] if models else None)
        if not model:
            raise RuntimeError("No models available from AIVM gateway")
        model_id = model["id"]
        print(f"  [AIVM] model: {model.get('name')} id={str(model_id)[:12]}…", flush=True)

        self._ensure_prepaid()

        # Draw a live worker. Slow on purpose (20–45s typical).
        try:
            draw = self._api_json(
                "POST", "/api/sessions/sortition/request",
                json_body={"modelId": model_id},
                timeout=AIVM_DRAW_TIMEOUT + 15,
            )
        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "timeout" in msg or "no answer" in msg:
                raise RuntimeError(
                    f"no worker is running {model.get('name')} at the moment") from e
            raise
        req_id = draw.get("reqId") or draw.get("requestId")
        if not req_id:
            raise RuntimeError("sortition draw returned no request id")
        self.last_worker = (draw.get("worker") or "").lower() or None
        print(f"  [AIVM] worker: {self.last_worker} reqId={str(req_id)[:12]}…", flush=True)

        session_key = secrets.token_bytes(32)
        enc_worker = _ecdh_wrap(session_key, _decode_pubkey(draw["workerEncryptionKey"]))
        enc_disputer = _ecdh_wrap(session_key, _decode_pubkey(draw["disputerEncryptionKey"]))

        opened = self._api_json(
            "POST", f"/api/sessions/sortition/{req_id}/keys",
            json_body={
                "encWorkerKey": _hex0x(enc_worker),
                "encDisputerKey": _hex0x(enc_disputer),
            },
            timeout=30,
        )
        session_id = opened.get("sessionId")
        if session_id is None or session_id == "":
            raise RuntimeError("sortition keys call created no session")
        print(f"  [AIVM] sessionId: {session_id} "
              f"tx={str(opened.get('txHash') or '')[:14]}", flush=True)

        relay_token = None
        for _ in range(30):
            r = req.get(
                f"{AIVM_GATEWAY}/api/sessions/{session_id}/token",
                headers=self._auth_headers(), timeout=10,
            )
            if r.status_code in (200, 202):
                try:
                    token = (r.json() or {}).get("token")
                except Exception:
                    token = None
                if token:
                    relay_token = token
                    break
            elif r.status_code >= 400:
                print(f"  [AIVM] relay token HTTP {r.status_code}: "
                      f"{self._api_error(r, 'token')}")
            time.sleep(2)
        if not relay_token:
            raise RuntimeError("Relay token not ready within 60s")

        chunks = []
        complete = threading.Event()
        ws_ready = threading.Event()
        ws_err = [None]
        seen_seq = set()

        def _on_message(ws_obj, message):
            try:
                frame = json.loads(message)
            except Exception:
                return
            payload = frame.get("payload")
            if payload:
                seq = frame.get("seq") if isinstance(frame.get("seq"), int) else payload
                if seq in seen_seq:
                    return
                try:
                    blob = base64.b64decode(payload)
                    pt = _aes_decrypt(session_key, blob)
                    text = pt.decode("utf-8", errors="replace")
                    stripped = text.strip()
                    # Worker telemetry frames decrypt but are not model text.
                    if stripped.startswith("{") and "promptTokens" in stripped:
                        return
                    chunks.append(text)
                    seen_seq.add(seq)
                except Exception:
                    pass
            ftype = frame.get("type")
            if ftype == "complete":
                complete.set()
            elif ftype == "error":
                ws_err[0] = frame.get("error") or "worker error"
                complete.set()

        def _on_open(ws_obj):
            ws_ready.set()

        def _on_error(ws_obj, err):
            ws_err[0] = err
            ws_ready.set()
            complete.set()

        ws = _ws.WebSocketApp(
            f"{AIVM_RELAY}?token={url_quote(relay_token)}",
            on_message=_on_message,
            on_open=_on_open,
            on_error=_on_error,
        )
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()
        ws_ready.wait(timeout=15)
        if ws_err[0] and not chunks:
            raise RuntimeError(f"WebSocket failed: {ws_err[0]}")
        print("  [AIVM] relay connected", flush=True)

        cipher = _aes_encrypt(session_key, prompt.encode("utf-8"))
        blob_body = self._api_json(
            "POST", "/api/blobs",
            json_body={
                "data": base64.b64encode(cipher).decode(),
                "sessionId": str(session_id),
            },
            timeout=30,
        )
        blob_hashes = blob_body.get("blobHashes") or []
        if not blob_hashes:
            raise RuntimeError("No blob hash returned from gateway")
        blob_hash = blob_hashes[0]

        submitted = self._api_json(
            "POST", f"/api/sessions/{session_id}/messages",
            json_body={"blobHash": blob_hash},
            timeout=30,
        )
        job_id = submitted.get("jobId")
        print(f"  [AIVM] jobId: {job_id}", flush=True)

        deadline = time.time() + timeout_secs
        while time.time() < deadline and not complete.is_set():
            time.sleep(1)
        time.sleep(2)
        try:
            ws.close()
        except Exception:
            pass

        result = "".join(chunks)
        if result:
            print(f"  [AIVM] inference done (relay data), {len(result)} chars", flush=True)
            return result
        if ws_err[0]:
            raise RuntimeError(f"AIVM worker error: {ws_err[0]}")
        raise RuntimeError(f"Timeout after {timeout_secs}s waiting for sortition answer")


_client = None
_client_lock = threading.Lock()


def configured():
    return bool(os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip())


def _ensure_client():
    global _client
    pk = os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip()
    if not pk:
        raise RuntimeError("AIVM is not configured")
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = AIVMClient(pk)
    return _client


def infer(prompt, timeout=360):
    return _ensure_client().run_inference(prompt, timeout_secs=timeout)


def infer_juror(prompt, timeout=360):
    """One inference that also reports which worker served it.

    Returns (text, worker_address). The worker address lets the jury draw 5
    DISTINCT jurors (dedupe by worker, redraw duplicates). Calls are serialized
    by the module-level AIVM tx lock, so jurors run one after another.
    """
    client = _ensure_client()
    text = client.run_inference(prompt, timeout_secs=timeout)
    return text, client.last_worker
