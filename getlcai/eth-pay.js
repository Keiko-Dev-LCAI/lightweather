/*
 * eth-pay.js — "Pay with Ethereum" helper for OrcaVault (and reusable across Orca/Light apps).
 *
 * Flow (all signed by the USER in their own wallet — non-custodial, no secrets, same
 * spirit as keiko-pay.js):
 *
 *   1. Swap ETH -> LCAI-ERC20 on Uniswap V3 (Ethereum).
 *   2. Bridge LCAI-ERC20 -> native LCAI via the official Lightchain Hyperlane warp route.
 *   3. Backend verifies the Ethereum-side bridge tx (transferRemote to the relay wallet,
 *      dest domain 9200, amount >= fee) and credits/unlocks the OrcaVault item. The bridge
 *      is NOT instant — the official Hyperlane relayer delivers native LCAI on the L1 side
 *      a short while later; the backend trusts that trusted route (same as bridge.lightchain.ai).
 *
 * Requires ethers v6 (BrowserProvider). Add via CDN in the page:
 *   <script src="https://cdnjs.cloudflare.com/ajax/libs/ethers/6.13.2/ethers.umd.min.js"></script>
 *
 * -----------------------------------------------------------------------------
 * WHY UNISWAP V3 (not V2)
 * -----------------------------------------------------------------------------
 *   LCAI's Ethereum liquidity lives in a Uniswap **V3** LCAI/WETH pool (0.3% fee tier,
 *   pool 0x0d047a370611437a1b8e6c2a95ea36f69fdda3be). There is NO Uniswap V2 LCAI pool,
 *   so the old V2 Router path reverted at the very first getAmountsIn() call
 *   ("execution reverted / no data present" = pair does not exist). This file therefore
 *   quotes via the V3 Quoter and swaps via the V3 SwapRouter.
 *
 * ADDRESS VERIFICATION STATUS
 *   LCAI_ERC20         VERIFIED  — official $LCAI contract on Ethereum.
 *   WETH               VERIFIED  — canonical Ethereum mainnet WETH9.
 *   UNISWAP_V3_ROUTER  canonical Uniswap V3 SwapRouter (mainnet periphery).
 *   UNISWAP_V3_QUOTER  canonical Uniswap V3 Quoter (mainnet periphery).
 *   LCAI_V3_FEE        3000 (0.3%) — the fee tier of the live LCAI/WETH V3 pool.
 *   LCAI_WARP_ROUTE    VERIFIED  — Ethereum-side Hyperlane collateral router (EvmHypCollateral)
 *                                  from lightchain-protocol/bridge-ui. transferRemote() target.
 *   LIGHTCHAIN_DOMAIN  VERIFIED  — Hyperlane destination domain id for Lightchain = 9200.
 *   (Native LCAI is minted on Lightchain by the EvmHypNative router
 *    0xEc7096A3116EE769457C939617375Ec1785AA6f1 — no direct call needed from this side.)
 * -----------------------------------------------------------------------------
 */
(function (global) {
  "use strict";

  // Resolve ethers from an isolated global first (set by the integration block so we never
  // clobber a host app that already loads its own ethers - e.g. OrcaMint on ethers v5),
  // falling back to window.ethers for pages that only have ours.
  function EL() {
    var e = global.__getlcaiEthers || global.ethers;
    if (!e) throw new Error("ethers v6 not loaded for Get LCAI.");
    return e;
  }

  var CFG = {
    // --- Ethereum side (chainId 1) ---
    ETH_CHAIN_ID_HEX: "0x1",
    LCAI_ERC20: "0x9cA8530CA349c966Fe9ef903Df17a75B8A778927", // VERIFIED
    WETH:        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", // VERIFIED

    // --- Uniswap V3 (LCAI liquidity is on V3, NOT V2) ---
    UNISWAP_V3_ROUTER: "0xE592427A0AEce92De3Edee1F18E0157C05861564", // canonical V3 SwapRouter (mainnet)
    UNISWAP_V3_QUOTER: "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6", // canonical V3 Quoter (mainnet)
    LCAI_V3_FEE: 3000, // 0.3% fee tier of the live LCAI/WETH V3 pool

    // --- The bridge (Hyperlane warp route, Ethereum -> Lightchain) ---
    LCAI_WARP_ROUTE: "0x01f80bb8e78e79881E8Ec7832fB6C2c59f64e353", // VERIFIED (EvmHypCollateral on Ethereum, wraps LCAI_ERC20)
    LIGHTCHAIN_DOMAIN: 9200, // VERIFIED (Hyperlane domainId for lcai, == chainId)

    // Slippage tolerance for the ETH->LCAI swap, in basis points (300 = 3%).
    SLIPPAGE_BPS: 300,
    // How long the swap tx stays valid, seconds.
    DEADLINE_SECS: 1200,
  };

  // Minimal ABIs (only the functions we call).
  // Uniswap V3 Quoter (QuoterV1). These are non-view in the ABI but are meant to be
  // eth_call'd — use .staticCall() in ethers v6.
  var ABI_V3_QUOTER = [
    "function quoteExactInputSingle(address tokenIn, address tokenOut, uint24 fee, uint256 amountIn, uint160 sqrtPriceLimitX96) returns (uint256 amountOut)",
    "function quoteExactOutputSingle(address tokenIn, address tokenOut, uint24 fee, uint256 amountOut, uint160 sqrtPriceLimitX96) returns (uint256 amountIn)"
  ];
  // Uniswap V3 SwapRouter (original ISwapRouter — struct includes deadline).
  var ABI_V3_ROUTER = [
    "function exactInputSingle((address tokenIn, address tokenOut, uint24 fee, address recipient, uint256 deadline, uint256 amountIn, uint256 amountOutMinimum, uint160 sqrtPriceLimitX96)) payable returns (uint256 amountOut)"
  ];
  var ABI_ERC20 = [
    "function balanceOf(address) view returns (uint256)",
    "function allowance(address owner, address spender) view returns (uint256)",
    "function approve(address spender, uint256 amount) returns (bool)"
  ];
  // Hyperlane warp route (TokenRouter). transferRemote is the canonical bridge call.
  var ABI_WARP = [
    "function quoteGasPayment(uint32 destinationDomain) view returns (uint256)",
    "function transferRemote(uint32 destination, bytes32 recipient, uint256 amount) payable returns (bytes32 messageId)"
  ];

  function addrToBytes32(addr) {
    // Hyperlane recipients are bytes32 (left-padded address).
    return "0x" + addr.toLowerCase().replace(/^0x/, "").padStart(64, "0");
  }

  async function getSigner() {
    var provider = global.ethereum;
    if (!provider) throw new Error("No wallet found. Install/enable MetaMask.");
    // Make sure we're on Ethereum mainnet for the swap + bridge legs.
    try {
      var cur = await provider.request({ method: "eth_chainId" });
      if (!cur || cur.toLowerCase() !== CFG.ETH_CHAIN_ID_HEX) {
        await provider.request({
          method: "wallet_switchEthereumChain",
          params: [{ chainId: CFG.ETH_CHAIN_ID_HEX }],
        });
      }
    } catch (e) { /* surface later if still wrong */ }
    var bp = new EL().BrowserProvider(provider);
    await bp.send("eth_requestAccounts", []);
    return bp.getSigner();
  }

  // Quote: how much LCAI you'd get for `ethAmount` (whole ETH string, e.g. "0.01").
  async function quoteLcaiForEth(ethAmount) {
    var signer = await getSigner();
    var quoter = new EL().Contract(CFG.UNISWAP_V3_QUOTER, ABI_V3_QUOTER, signer);
    var amountIn = EL().parseEther(String(ethAmount));
    var out = await quoter.quoteExactInputSingle.staticCall(
      CFG.WETH, CFG.LCAI_ERC20, CFG.LCAI_V3_FEE, amountIn, 0n
    );
    return out; // LCAI out (BigInt, 18 decimals)
  }

  // Reverse quote: how much ETH (whole-ETH string) to buy `lcaiAmount` LCAI (whole-token
  // string), padded by `bufferPct` (e.g. 0.15 = +15%) to absorb slippage + bridge gas.
  async function ethForLcai(lcaiAmount, bufferPct) {
    var signer = await getSigner();
    var quoter = new EL().Contract(CFG.UNISWAP_V3_QUOTER, ABI_V3_QUOTER, signer);
    var lcaiOut = EL().parseEther(String(lcaiAmount)); // 18 decimals
    var ethIn = await quoter.quoteExactOutputSingle.staticCall(
      CFG.WETH, CFG.LCAI_ERC20, CFG.LCAI_V3_FEE, lcaiOut, 0n
    ); // WETH in (BigInt)
    var pad = BigInt(Math.round((Number(bufferPct) || 0) * 10000));
    var padded = ethIn + (ethIn * pad) / 10000n;
    return EL().formatEther(padded); // whole-ETH string
  }

  // Step 1: swap ETH -> LCAI (Uniswap V3), delivered to the user's own address.
  // Returns the ACTUAL LCAI received (balance delta) so the bridge can move exactly that.
  async function swapEthForLcai(ethAmount, onStatus) {
    var signer = await getSigner();
    var me = await signer.getAddress();
    var router = new EL().Contract(CFG.UNISWAP_V3_ROUTER, ABI_V3_ROUTER, signer);
    var quoter = new EL().Contract(CFG.UNISWAP_V3_QUOTER, ABI_V3_QUOTER, signer);
    var lcai   = new EL().Contract(CFG.LCAI_ERC20, ABI_ERC20, signer);
    var amountIn = EL().parseEther(String(ethAmount));

    // Expected LCAI out for this ETH, then apply slippage floor.
    var expected = await quoter.quoteExactInputSingle.staticCall(
      CFG.WETH, CFG.LCAI_ERC20, CFG.LCAI_V3_FEE, amountIn, 0n
    );
    var minOut = expected - (expected * BigInt(CFG.SLIPPAGE_BPS)) / 10000n;
    var deadline = Math.floor(Date.now() / 1000) + CFG.DEADLINE_SECS;

    var balBefore = await lcai.balanceOf(me);

    if (onStatus) onStatus("swap", "Swapping ETH → LCAI on Uniswap…");
    var params = {
      tokenIn: CFG.WETH,
      tokenOut: CFG.LCAI_ERC20,
      fee: CFG.LCAI_V3_FEE,
      recipient: me,
      deadline: deadline,
      amountIn: amountIn,
      amountOutMinimum: minOut,
      sqrtPriceLimitX96: 0n
    };
    var tx = await router.exactInputSingle(params, { value: amountIn });
    await tx.wait();

    var balAfter = await lcai.balanceOf(me);
    var received = balAfter - balBefore;
    if (received <= 0n) received = minOut; // fallback; swap would have reverted if under minOut
    return { txHash: tx.hash, expectedLcai: expected, minLcai: minOut, receivedLcai: received };
  }

  // Step 2: bridge LCAI-ERC20 -> native LCAI on Lightchain, to `recipientL1`
  // (defaults to the same wallet address on the L1 side).
  async function bridgeLcaiToNative(lcaiAmount /* BigInt */, recipientL1, onStatus) {
    if (CFG.LCAI_WARP_ROUTE === "0x0000000000000000000000000000000000000000") {
      throw new Error("LCAI_WARP_ROUTE not set — pull the real warp-route address from the bridge first.");
    }
    var signer = await getSigner();
    var me = await signer.getAddress();
    var to = recipientL1 || me;

    var lcai = new EL().Contract(CFG.LCAI_ERC20, ABI_ERC20, signer);
    var warp = new EL().Contract(CFG.LCAI_WARP_ROUTE, ABI_WARP, signer);

    // Approve the warp route to pull the LCAI (only if needed).
    var allowance = await lcai.allowance(me, CFG.LCAI_WARP_ROUTE);
    if (allowance < lcaiAmount) {
      if (onStatus) onStatus("approve", "Approving LCAI for the bridge…");
      var ap = await lcai.approve(CFG.LCAI_WARP_ROUTE, lcaiAmount);
      await ap.wait();
    }

    // Hyperlane charges an interchain gas payment (paid in ETH via msg.value).
    var gasPay = 0n;
    try { gasPay = await warp.quoteGasPayment(CFG.LIGHTCHAIN_DOMAIN); } catch (e) { /* some routes are 0 */ }

    if (onStatus) onStatus("bridge", "Bridging LCAI → native LCAI…");
    var tx = await warp.transferRemote(
      CFG.LIGHTCHAIN_DOMAIN,
      addrToBytes32(to),
      lcaiAmount,
      { value: gasPay }
    );
    var rc = await tx.wait();
    return { txHash: tx.hash, receipt: rc };
  }

  /*
   * Orchestrator. Runs swap -> bridge, then hands the details to the app's backend so
   * it can verify the Ethereum-side bridge tx and unlock the item.
   *
   *   payWithEth({
   *     ethAmount:  "0.01",                 // what the user deposits
   *     recipientL1:"0xRelayOrVaultWallet", // where native LCAI should land
   *     verifyUrl:  "/api/register-eth-payment",
   *     meta:       { app: "orcavault" }
   *   }, onStatus)
   *
   * NOTE: the bridge is async. This resolves once the swap + bridge txs are submitted
   * and confirmed on Ethereum; the native LCAI lands on L1 a short while later. The
   * backend (verifyUrl) verifies the bridge tx and flips the unlock.
   */
  async function payWithEth(opts, onStatus) {
    opts = opts || {};
    if (!opts.ethAmount) return { ok: false, error: "Missing ethAmount." };
    try {
      var swap = await swapEthForLcai(opts.ethAmount, onStatus);
      var bridgeAmount = swap.receivedLcai; // bridge exactly what we actually got
      var bridge = await bridgeLcaiToNative(bridgeAmount, opts.recipientL1, onStatus);

      if (onStatus) onStatus("pending", "Bridging… native LCAI will land shortly.");

      if (opts.verifyUrl) {
        var signer = await getSigner();
        var from = await signer.getAddress();
        var body = {
          walletAddress: from,
          recipientL1: opts.recipientL1 || from,
          swapTxHash: swap.txHash,
          bridgeTxHash: bridge.txHash,
          lcaiAmount: bridgeAmount.toString(),
          meta: opts.meta || null,
        };
        var resp = await fetch(opts.verifyUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        var out = await resp.json().catch(function () { return {}; });
        if (!resp.ok || out.error) {
          return { ok: false, error: out.error || "Backend could not record the bridge.", swapTxHash: swap.txHash, bridgeTxHash: bridge.txHash };
        }
        return { ok: true, pending: true, swapTxHash: swap.txHash, bridgeTxHash: bridge.txHash, result: out };
      }
      return { ok: true, pending: true, swapTxHash: swap.txHash, bridgeTxHash: bridge.txHash };
    } catch (e) {
      return { ok: false, error: (e && e.message) || "Payment cancelled or failed." };
    }
  }

  global.EthPay = {
    pay: payWithEth,
    quote: quoteLcaiForEth,
    ethForLcai: ethForLcai,
    swapEthForLcai: swapEthForLcai,
    bridgeLcaiToNative: bridgeLcaiToNative,
    config: CFG,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = global.EthPay;
})(typeof window !== "undefined" ? window : this);
