#!/usr/bin/env python3
"""LightWeather — Backend server. Weather data + AIVM forecast explanation + premium subscriptions."""

import os, time, json, threading, sqlite3
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

# ── AIVM ─────────────────────────────────────────────────────────────────────
# Live path is aivm_client.py: sortition request, keys, prepaid balance, and
# the relay. The retired /api/sessions/select + /api/sessions/prepare handshake
# is not called.
AIVM_PRIVATE_KEY = os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip()

class AIVMClient:
    def __init__(self, private_key):
        if not private_key:
            raise RuntimeError("AIVM is not configured")

    def run_inference(self, prompt, timeout_secs=240):
        import aivm_client as _aivm_sortition
        return _aivm_sortition.infer(prompt, timeout=timeout_secs)

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
        return jsonify({'error': err, 'explanation': _fallback_explanation(summary, language), 'fallback': True}), code
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
        return jsonify({'explanation': _fallback_explanation(summary, language), 'fallback': True})

    try:
        # Stay under the gunicorn --timeout 240 so this returns the fallback
        # instead of the worker being killed mid-request.
        result = (client.run_inference(prompt, timeout_secs=180) or "").strip()
        if not result or result == "No response from AIVM worker":
            return jsonify({'explanation': _fallback_explanation(summary, language), 'fallback': True})
        return jsonify({'explanation': result})
    except Exception as e:
        print(f"[AIVM] error: {e}")
        return jsonify({'explanation': _fallback_explanation(summary, language), 'fallback': True})

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
