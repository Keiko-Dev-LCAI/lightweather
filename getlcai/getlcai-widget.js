/*
 * getlcai-widget.js — drop-in "Get LCAI with ETH" on-ramp for any Orca/Light app.
 *
 * Lets a user who only holds ETH convert to native LCAI (Uniswap V3 swap + official
 * Lightchain Hyperlane bridge), delivered to their OWN wallet. No backend needed —
 * bridges to self, so the user then holds native LCAI to pay/transact in the host app.
 *
 * Non-custodial: the user signs every step. This widget never holds funds or secrets.
 *
 * INTEGRATION (add these three lines before </body>, in this order):
 *   <script src="./getlcai/ethers-6.13.2.umd.min.js"></script>
 *   <script src="./getlcai/eth-pay.js"></script>
 *   <script src="./getlcai/getlcai-widget.js"></script>
 *
 * By default it renders a floating "Get LCAI" launcher button (bottom-right) that opens
 * a modal with the full flow. To mount inline instead, put an element with id
 * "getlcai-mount" (or attribute data-getlcai) anywhere in the page and the widget renders
 * a button into it in place of the floating one.
 *
 * All styles are namespaced with the .glw- prefix and injected into a scoped block so the
 * widget does not collide with the host app's CSS.
 */
(function (global) {
  "use strict";

  if (global.__getLcaiWidgetLoaded) return;
  global.__getLcaiWidgetLoaded = true;

  function EL() { var e = global.__getlcaiEthers || global.ethers; if (!e) throw new Error("ethers v6 not loaded for Get LCAI."); return e; }

  var CSS = ''
    + '.glw-launch{position:fixed;right:18px;bottom:18px;z-index:2147483000;background:linear-gradient(180deg,#f0c65e,#e0a52f);'
    + 'color:#1a1206;border:0;border-radius:999px;padding:12px 18px;font-size:15px;font-weight:800;cursor:pointer;'
    + 'box-shadow:0 8px 28px rgba(0,0,0,.4);font-family:inherit;display:flex;align-items:center;gap:8px}'
    + '.glw-launch:active{transform:scale(.98)}'
    + '.glw-inline{background:linear-gradient(180deg,#f0c65e,#e0a52f);color:#1a1206;border:0;border-radius:12px;'
    + 'padding:13px 18px;font-size:15px;font-weight:800;cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:8px}'
    + '.glw-overlay{position:fixed;inset:0;z-index:2147483001;background:rgba(4,6,10,.72);backdrop-filter:blur(3px);'
    + 'display:flex;align-items:center;justify-content:center;padding:16px}'
    + '.glw-overlay.glw-hidden{display:none}'
    + '.glw-modal{width:100%;max-width:440px;max-height:92vh;overflow:auto;background:#151a26;border:1px solid #232a3a;'
    + 'border-radius:18px;padding:20px;color:#eef2f8;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;'
    + 'box-shadow:0 20px 60px rgba(0,0,0,.5)}'
    + '.glw-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:14px}'
    + '.glw-title{font-size:20px;font-weight:800;margin:0}'
    + '.glw-sub{color:#9aa6b8;font-size:13px;line-height:1.5;margin:4px 0 0}'
    + '.glw-x{background:transparent;border:0;color:#9aa6b8;font-size:24px;line-height:1;cursor:pointer;padding:0 4px}'
    + '.glw-conn{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#121620;border:1px solid #232a3a;'
    + 'border-radius:12px;padding:10px 12px;font-size:13px;margin-bottom:12px}'
    + '.glw-conn .glw-addr{font-family:monospace;color:#e8b84b}'
    + '.glw-conn .glw-bal{color:#9aa6b8}'
    + '.glw-label{display:block;font-size:12px;color:#9aa6b8;margin:0 0 7px;font-weight:600;text-transform:uppercase;letter-spacing:.6px}'
    + '.glw-amt{display:flex;align-items:center;background:#121620;border:1.5px solid #232a3a;border-radius:12px;padding:2px 12px}'
    + '.glw-amt input{flex:1;background:transparent;border:0;color:#eef2f8;font-size:24px;font-weight:700;padding:11px 0;outline:none;width:100%}'
    + '.glw-amt .glw-unit{font-size:16px;font-weight:700;color:#9aa6b8}'
    + '.glw-picks{display:flex;gap:8px;margin-top:10px}'
    + '.glw-pick{flex:1;background:#121620;border:1px solid #232a3a;color:#eef2f8;border-radius:10px;padding:8px 0;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}'
    + '.glw-pick:hover{border-color:#e8b84b}'
    + '.glw-quote{margin-top:12px;font-size:14px;color:#39d0d8;min-height:18px;text-align:center}'
    + '.glw-btn{width:100%;border:0;border-radius:12px;padding:15px;font-size:17px;font-weight:800;cursor:pointer;font-family:inherit;'
    + 'display:flex;align-items:center;justify-content:center;gap:8px;margin-top:14px}'
    + '.glw-btn:disabled{opacity:.5;cursor:not-allowed}'
    + '.glw-gold{background:linear-gradient(180deg,#f0c65e,#e0a52f);color:#1a1206}'
    + '.glw-steps{margin-top:14px}'
    + '.glw-step{display:flex;align-items:center;gap:11px;padding:9px 0;color:#9aa6b8;font-size:14px;border-top:1px solid #232a3a}'
    + '.glw-step:first-child{border-top:0}'
    + '.glw-step .glw-dot{width:20px;height:20px;border-radius:50%;border:2px solid #232a3a;flex:0 0 auto;display:flex;align-items:center;justify-content:center;font-size:11px}'
    + '.glw-step.glw-active{color:#eef2f8}.glw-step.glw-active .glw-dot{border-color:#e8b84b;color:#e8b84b}'
    + '.glw-step.glw-done{color:#3ddc84}.glw-step.glw-done .glw-dot{border-color:#3ddc84;background:#3ddc84;color:#0b0e14}'
    + '.glw-spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.25);border-top-color:#e8b84b;border-radius:50%;display:inline-block;animation:glw-sp .7s linear infinite}'
    + '@keyframes glw-sp{to{transform:rotate(360deg)}}'
    + '.glw-note{font-size:12px;color:#9aa6b8;line-height:1.55;margin-top:12px;text-align:center}'
    + '.glw-result{margin-top:12px;padding:13px;background:rgba(61,220,132,.08);border:1px solid rgba(61,220,132,.3);border-radius:12px}'
    + '.glw-result a{color:#39d0d8;text-decoration:none;word-break:break-all;font-size:13px}'
    + '.glw-msg{margin-top:10px;font-size:13px;text-align:center;min-height:16px}'
    + '.glw-msg.glw-err{color:#e85252}'
    + '.glw-hidden{display:none!important}';

  var MODAL_HTML = ''
    + '<div class="glw-overlay glw-hidden" id="glw-overlay">'
    + '  <div class="glw-modal" role="dialog" aria-label="Get LCAI with ETH">'
    + '    <div class="glw-head">'
    + '      <div><h3 class="glw-title">🐋 Get LCAI with ETH</h3>'
    + '      <p class="glw-sub">Only hold ETH? Convert to native LCAI in one flow — swap on Uniswap + bridge, delivered to your own wallet. Non-custodial: you sign every step.</p></div>'
    + '      <button class="glw-x" id="glw-close" aria-label="Close">&times;</button>'
    + '    </div>'
    + '    <button class="glw-btn glw-gold" id="glw-connect">🔌 Connect Wallet</button>'
    + '    <div class="glw-conn glw-hidden" id="glw-conn"><span>Connected <span class="glw-addr" id="glw-addr"></span></span><span class="glw-bal" id="glw-bal"></span></div>'
    + '    <div id="glw-buy" style="margin-top:12px">'
    + '      <label class="glw-label" for="glw-eth">You pay</label>'
    + '      <div class="glw-amt"><input id="glw-eth" type="number" inputmode="decimal" min="0" step="0.001" placeholder="0.0"><span class="glw-unit">ETH</span></div>'
    + '      <div class="glw-picks"><button class="glw-pick" data-v="0.01">0.01</button><button class="glw-pick" data-v="0.05">0.05</button><button class="glw-pick" data-v="0.1">0.1</button></div>'
    + '      <div class="glw-quote" id="glw-quote"></div>'
    + '      <div class="glw-steps glw-hidden" id="glw-steps">'
    + '        <div class="glw-step" id="glw-s-swap"><span class="glw-dot">1</span><span>Swap ETH → LCAI on Uniswap</span></div>'
    + '        <div class="glw-step" id="glw-s-approve"><span class="glw-dot">2</span><span>Approve LCAI for the bridge</span></div>'
    + '        <div class="glw-step" id="glw-s-bridge"><span class="glw-dot">3</span><span>Bridge → native LCAI</span></div>'
    + '      </div>'
    + '      <button class="glw-btn glw-gold" id="glw-get" disabled>Get LCAI</button>'
    + '      <div class="glw-msg" id="glw-msg"></div>'
    + '      <div class="glw-result glw-hidden" id="glw-result"></div>'
    + '      <p class="glw-note">You\'ll sign 2–3 transactions and pay normal Ethereum gas. Native LCAI lands in your wallet a few minutes after the bridge confirms. Not financial advice.</p>'
    + '    </div>'
    + '  </div>'
    + '</div>';

  var myAddress = null, quoteTimer = null, busy = false, mounted = false;

  function el(id) { return document.getElementById(id); }
  function short(a) { return a ? a.slice(0, 6) + '…' + a.slice(-4) : ''; }
  function setMsg(t, isErr) { var m = el('glw-msg'); if (!m) return; m.textContent = t || ''; m.className = 'glw-msg' + (isErr ? ' glw-err' : ''); }

  function open() { var o = el('glw-overlay'); if (o) o.classList.remove('glw-hidden'); }
  function close() { var o = el('glw-overlay'); if (o) o.classList.add('glw-hidden'); }

  async function connect() {
    try {
      if (!global.ethereum) { setMsg('No wallet found. Install or enable MetaMask.', true); return; }
      var bp = new EL().BrowserProvider(global.ethereum);
      await bp.send('eth_requestAccounts', []);
      var signer = await bp.getSigner();
      myAddress = await signer.getAddress();
      el('glw-addr').textContent = short(myAddress);
      el('glw-conn').classList.remove('glw-hidden');
      el('glw-connect').classList.add('glw-hidden');
      try { var bal = await bp.getBalance(myAddress); el('glw-bal').textContent = (+EL().formatEther(bal)).toFixed(4) + ' ETH'; } catch (e) {}
      refreshButton(); setMsg('');
    } catch (e) { setMsg('Could not connect: ' + ((e && e.message) || e), true); }
  }

  function onAmount() {
    refreshButton();
    var v = parseFloat(el('glw-eth').value);
    el('glw-quote').textContent = '';
    if (!v || v <= 0 || !global.EthPay) return;
    clearTimeout(quoteTimer);
    el('glw-quote').textContent = 'Getting a price…';
    quoteTimer = setTimeout(async function () {
      try {
        var out = await global.EthPay.quote(String(v));
        var lcai = (+EL().formatEther(out));
        el('glw-quote').textContent = '≈ ' + lcai.toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' native LCAI';
      } catch (e) { el('glw-quote').textContent = ''; }
    }, 500);
  }

  function refreshButton() {
    var v = parseFloat(el('glw-eth').value);
    var b = el('glw-get'); if (b) b.disabled = busy || !myAddress || !v || v <= 0;
  }

  function markStep(id, state) {
    var s = el(id); if (!s) return;
    s.className = 'glw-step ' + (state ? 'glw-' + state : '');
    var dot = s.querySelector('.glw-dot');
    if (state === 'done') dot.innerHTML = '✓';
    else if (state === 'active') dot.innerHTML = '<span class="glw-spin"></span>';
    else dot.textContent = id === 'glw-s-swap' ? '1' : id === 'glw-s-approve' ? '2' : '3';
  }

  async function getLcai() {
    if (busy) return;
    var v = parseFloat(el('glw-eth').value);
    if (!myAddress) { setMsg('Connect your wallet first.', true); return; }
    if (!v || v <= 0) { setMsg('Enter an ETH amount.', true); return; }
    if (!global.EthPay) { setMsg('Payment engine not loaded — refresh and retry.', true); return; }

    busy = true; refreshButton();
    el('glw-result').classList.add('glw-hidden');
    el('glw-steps').classList.remove('glw-hidden');
    markStep('glw-s-swap', 'active'); markStep('glw-s-approve', ''); markStep('glw-s-bridge', '');
    setMsg('');
    try {
      var swap = await global.EthPay.swapEthForLcai(String(v), function (phase, m) { if (phase === 'swap') setMsg(m); });
      markStep('glw-s-swap', 'done');
      var bridge = await global.EthPay.bridgeLcaiToNative(swap.receivedLcai, myAddress, function (phase, m) {
        if (phase === 'approve') { markStep('glw-s-approve', 'active'); setMsg(m); }
        if (phase === 'bridge') { markStep('glw-s-approve', 'done'); markStep('glw-s-bridge', 'active'); setMsg(m); }
      });
      markStep('glw-s-approve', 'done'); markStep('glw-s-bridge', 'done');
      var got = (+EL().formatEther(swap.receivedLcai)).toLocaleString(undefined, { maximumFractionDigits: 2 });
      setMsg('');
      el('glw-result').classList.remove('glw-hidden');
      el('glw-result').innerHTML =
        '<div style="font-weight:700;color:#3ddc84;margin-bottom:8px">✅ Done — ' + got + ' native LCAI is on its way to your wallet</div>'
        + '<div style="color:#9aa6b8;font-size:12px;margin-bottom:8px">It lands in a few minutes once the bridge relayer delivers.</div>'
        + '<div><a href="https://etherscan.io/tx/' + swap.txHash + '" target="_blank" rel="noopener">Swap transaction ↗</a></div>'
        + '<div style="margin-top:4px"><a href="https://etherscan.io/tx/' + bridge.txHash + '" target="_blank" rel="noopener">Bridge transaction ↗</a></div>';
    } catch (e) {
      var m = (e && (e.shortMessage || e.message)) || String(e);
      if (/reject|denied|4001/i.test(m)) setMsg('Cancelled — nothing was sent. Try again anytime.', true);
      else setMsg("Didn't go through: " + m, true);
      ['glw-s-swap', 'glw-s-approve', 'glw-s-bridge'].forEach(function (id) { var s = el(id); if (s && s.classList.contains('glw-active')) markStep(id, ''); });
    } finally { busy = false; refreshButton(); }
  }

  function mount() {
    if (mounted) return;
    mounted = true;

    var style = document.createElement('style');
    style.setAttribute('data-glw', '1');
    style.textContent = CSS;
    document.head.appendChild(style);

    var modalWrap = document.createElement('div');
    modalWrap.innerHTML = MODAL_HTML;
    document.body.appendChild(modalWrap);

    // Launcher: inline mount point if present, else floating button.
    var host = document.getElementById('getlcai-mount') || document.querySelector('[data-getlcai]');
    var launch = document.createElement('button');
    launch.textContent = '🐋 Get LCAI';
    if (host) { launch.className = 'glw-inline'; host.appendChild(launch); }
    else { launch.className = 'glw-launch'; document.body.appendChild(launch); }
    launch.addEventListener('click', open);

    el('glw-close').addEventListener('click', close);
    el('glw-overlay').addEventListener('click', function (e) { if (e.target === el('glw-overlay')) close(); });
    el('glw-connect').addEventListener('click', connect);
    el('glw-get').addEventListener('click', getLcai);
    el('glw-eth').addEventListener('input', onAmount);
    var picks = document.querySelectorAll('.glw-pick');
    for (var i = 0; i < picks.length; i++) {
      picks[i].addEventListener('click', function () { el('glw-eth').value = this.getAttribute('data-v'); onAmount(); });
    }
    if (global.ethereum && global.ethereum.on) {
      global.ethereum.on('accountsChanged', function () {
        myAddress = null;
        var c = el('glw-conn'), cb = el('glw-connect');
        if (c) c.classList.add('glw-hidden'); if (cb) cb.classList.remove('glw-hidden');
        refreshButton();
      });
    }
  }

  // Public API so a host app can open the on-ramp from its own "insufficient LCAI" prompt.
  global.GetLcaiWidget = { open: open, close: close, mount: mount };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})(typeof window !== "undefined" ? window : this);
