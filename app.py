"""
ARB TRIANGULAR BOT v4 — Ficheiro unico, pronto para Render.com
Todos os ficheiros num so: sem erros de caminhos, sem dependencias externas.

DEPLOY RENDER:
  Build: pip install -r requirements.txt
  Start: gunicorn app:app
"""
from flask import Flask, jsonify, request
import ccxt, threading, time, os
from datetime import datetime
from collections import defaultdict

app = Flask(__name__)

# ═══════════════════════════════════════════
#  ESTADO GLOBAL
# ═══════════════════════════════════════════
BOT = {
    "running": False, "paper": True,
    "capital": 10.0, "cap_inicial": 10.0, "cap_base": 10.0,
    "saldo_conta": 10.0,
    "lucro_total": 0.0, "lucro_ciclo": 0.0,
    "ciclos_jc": 0, "gatilho_jc": 10.0,
    "arbs_exec": 0, "arbs_achadas": 0, "arbs_rejeit": 0,
    "scans": 0, "melhor": 0.0, "drawdown": 0.0,
    "lucro_min": 0.20, "slip_max": 0.05, "liq_min": 500, "max_dd": 10.0,
    "api_key":    os.getenv("BINANCE_API_KEY", ""),
    "api_secret": os.getenv("BINANCE_API_SECRET", ""),
    "cooldowns": {}, "logs": [], "scan_data": [], "last_arb": None, "marcos": [],
    "arbs_hora": 0, "hora_atual": datetime.now().hour,
}
TAXA = 0.00075
ex = None
_lock = threading.Lock()

TRIANGULOS = [
    ["USDT","BTC","ETH"],["USDT","BTC","BNB"],["USDT","ETH","BNB"],
    ["USDT","BTC","SOL"],["USDT","ETH","SOL"],["USDT","BNB","SOL"],
    ["USDT","BTC","XRP"],["USDT","ETH","XRP"],["USDT","BTC","ADA"],
    ["USDT","BTC","DOGE"],["USDT","ETH","DOGE"],["USDT","BNB","XRP"],
    ["USDT","BTC","AVAX"],["USDT","ETH","AVAX"],["USDT","BTC","LINK"],
    ["USDT","ETH","LINK"],["USDT","BTC","MATIC"],["USDT","ETH","MATIC"],
    ["USDT","BNB","MATIC"],["USDT","BTC","DOT"],
]

def add_log(msg, t="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        BOT["logs"].insert(0, {"ts": ts, "msg": msg, "t": t})
        BOT["logs"] = BOT["logs"][:200]
    print(f"{ts} | {msg}")

def preco_ob(par, lado, usdt):
    try:
        ob = ex.fetch_order_book(par, limit=10)
        ns = ob["asks"] if lado == "c" else ob["bids"]
        if not ns: return None, None, None
        best = float(ns[0][0])
        liq  = sum(float(p) * float(q) for p, q in ns)
        if liq < BOT["liq_min"]: return None, None, liq
        acum = custo = 0.0
        for p, q in ns:
            p, q = float(p), float(q)
            v = p * q
            if acum + v >= usdt:
                custo += usdt - acum; acum = usdt; break
            custo += v; acum += v
        if acum < usdt * 0.99: return None, None, liq
        med  = custo / (acum / best)
        slip = abs(med - best) / best * 100
        return med, slip, liq
    except:
        return None, None, None

def calcular(tri, capital):
    base, A, B = tri
    try:
        p1, s1, l1 = preco_ob(f"{A}/{base}", "c", capital)
        if p1 is None: return None
        qa = (capital / p1) * (1 - TAXA)

        p2, s2, l2 = preco_ob(f"{B}/{A}", "c", qa * p1 * (1 - TAXA))
        if p2 is None: return None
        qb = (qa / p2) * (1 - TAXA)

        p3, s3, l3 = preco_ob(f"{B}/{base}", "v", qb * p2 * p1 * (1 - TAXA))
        if p3 is None: return None
        final = qb * p3 * (1 - TAXA)

        lucro = final - capital
        pct   = lucro / capital * 100
        slip  = (s1 or 0) + (s2 or 0) + (s3 or 0)
        lmin  = min(l1 or 0, l2 or 0, l3 or 0)
        return {
            "tri":    f"{base}>{A}>{B}>{base}",
            "pares":  [f"{A}/{base}", f"{B}/{A}", f"{B}/{base}"],
            "precos": [p1, p2, p3],
            "qtds":   [qa, qb, final],
            "capital": capital,
            "lucro":  round(lucro, 8),
            "pct":    round(pct, 6),
            "slip":   round(slip, 6),
            "lmin":   round(lmin, 2),
            "ok":     pct >= BOT["lucro_min"] and slip <= BOT["slip_max"] and lmin >= BOT["liq_min"],
        }
    except:
        return None

def registar_lucro(lucro):
    BOT["capital"]     += lucro
    BOT["lucro_total"] += lucro
    BOT["lucro_ciclo"] += lucro
    BOT["saldo_conta"] += lucro
    g = BOT["cap_base"] * (BOT["gatilho_jc"] / 100)
    if BOT["lucro_ciclo"] >= g:
        antes  = BOT["cap_base"]
        depois = BOT["capital"]
        lc     = BOT["lucro_ciclo"]
        ganho  = lc / antes * 100
        BOT["ciclos_jc"]  += 1
        BOT["cap_base"]    = depois
        BOT["lucro_ciclo"] = 0.0
        BOT["marcos"].insert(0, {
            "ciclo":  BOT["ciclos_jc"],
            "antes":  round(antes, 6),
            "depois": round(depois, 6),
            "lucro":  round(lc, 6),
            "ganho":  round(ganho, 4),
            "data":   datetime.now().strftime("%d/%m %H:%M"),
        })
        BOT["marcos"] = BOT["marcos"][:20]
        add_log(f"JUROS COMPOSTOS #{BOT['ciclos_jc']} | ${antes:.4f} > ${depois:.4f} (+{ganho:.4f}%)", "compound")

def executar_arb(res):
    if BOT["paper"]:
        add_log(f"SIM | {res['tri']} | +${res['lucro']:.6f} (+{res['pct']:.4f}%) | slip {res['slip']:.4f}%", "success")
        return True, res["lucro"]
    par1, par2, par3 = res["pares"]
    p1, p2, _        = res["precos"]
    qa, qb, _        = res["qtds"]
    try:
        t0 = time.time()
        o1 = ex.create_market_order(par1, "buy",  res["capital"] / p1); time.sleep(0.08)
        o2 = ex.create_market_order(par2, "buy",  float(o1.get("filled", qa)) / p2); time.sleep(0.08)
        o3 = ex.create_market_order(par3, "sell", float(o2.get("filled", qb)))
        lr = float(o3.get("cost", 0)) - res["capital"]
        add_log(f"ARB REAL {time.time()-t0:.2f}s | Lucro: ${lr:+.6f}", "success")
        return True, lr
    except ccxt.InsufficientFunds:
        add_log("Saldo insuficiente", "error"); return False, 0
    except Exception as e:
        add_log(f"Erro execucao: {e}", "error"); return False, 0

def bot_loop():
    global ex
    add_log(f"Bot iniciado | ${BOT['capital']} | JC {BOT['gatilho_jc']}% | {'SIM' if BOT['paper'] else 'REAL'}", "success")
    ex = ccxt.binance({
        "apiKey": BOT["api_key"], "secret": BOT["api_secret"],
        "enableRateLimit": True, "options": {"defaultType": "spot"},
    })
    if not BOT["paper"] and BOT["api_key"]:
        try:
            bal = ex.fetch_balance()
            BOT["saldo_conta"] = float(bal.get("USDT", {}).get("free", BOT["capital"]))
            add_log(f"Saldo Binance: ${BOT['saldo_conta']:.4f} USDT", "info")
        except Exception as e:
            add_log(f"Aviso saldo: {e}", "warn")

    ult_saldo = time.time()
    while BOT["running"]:
        try:
            if BOT["cap_inicial"] > 0:
                dd = (BOT["cap_inicial"] - BOT["capital"]) / BOT["cap_inicial"] * 100
                BOT["drawdown"] = max(0, dd)
                if dd >= BOT["max_dd"]:
                    add_log(f"DRAWDOWN {dd:.2f}% — Bot parado!", "error")
                    BOT["running"] = False
                    break

            h = datetime.now().hour
            if h != BOT["hora_atual"]:
                BOT["hora_atual"] = h
                BOT["arbs_hora"]  = 0
            if BOT["arbs_hora"] >= 20:
                time.sleep(60); continue

            if not BOT["paper"] and time.time() - ult_saldo > 120:
                try:
                    bal = ex.fetch_balance()
                    BOT["saldo_conta"] = float(bal.get("USDT", {}).get("free", BOT["saldo_conta"]))
                except: pass
                ult_saldo = time.time()

            BOT["scans"] += 1
            ops = []
            scan_todos = []

            for tri in TRIANGULOS:
                tri_str = f"{tri[0]}>{tri[1]}>{tri[2]}>{tri[0]}"
                cd   = BOT["cooldowns"].get(tri_str, 0)
                skip = (time.time() - cd) < 30
                res  = calcular(tri, BOT["capital"])
                if res is None: continue
                if res["pct"] > BOT["melhor"]: BOT["melhor"] = res["pct"]
                scan_todos.append(res)
                if not skip and res["ok"]:
                    ops.append(res); BOT["arbs_achadas"] += 1
                elif res["pct"] > 0 and not res["ok"]:
                    BOT["arbs_rejeit"] += 1

            scan_todos.sort(key=lambda x: x["pct"], reverse=True)
            BOT["scan_data"] = scan_todos[:20]

            if ops:
                ops.sort(key=lambda x: x["pct"] - x["slip"] * 2, reverse=True)
                melhor = ops[0]
                partes = melhor["tri"].split(">")
                check  = calcular(partes[:3], melhor["capital"])
                if check and check["ok"] and abs(check["pct"] - melhor["pct"]) < 0.3:
                    ok, lucro = executar_arb(melhor)
                    if ok:
                        registar_lucro(lucro)
                        BOT["arbs_exec"]  += 1
                        BOT["arbs_hora"]  += 1
                        BOT["last_arb"]    = melhor
                        BOT["cooldowns"][melhor["tri"]] = time.time()

            time.sleep(2)

        except ccxt.NetworkError as e:
            add_log(f"Rede: {e}", "warn"); time.sleep(15)
        except ccxt.RateLimitExceeded:
            add_log("Rate limit", "warn"); time.sleep(30)
        except Exception as e:
            add_log(f"Erro: {e}", "error"); time.sleep(10)

    add_log("Bot parado", "warn")

# ═══════════════════════════════════════════
#  API ROUTES
# ═══════════════════════════════════════════
@app.route("/api/status")
def api_status():
    roi  = (BOT["capital"] / BOT["cap_inicial"] - 1) * 100 if BOT["cap_inicial"] > 0 else 0
    g    = BOT["cap_base"] * BOT["gatilho_jc"] / 100
    prog = min(100, BOT["lucro_ciclo"] / g * 100) if g > 0 else 0
    return jsonify({
        "running":      BOT["running"],
        "paper":        BOT["paper"],
        "capital":      round(BOT["capital"], 6),
        "saldo_conta":  round(BOT["saldo_conta"], 4),
        "lucro_total":  round(BOT["lucro_total"], 6),
        "lucro_ciclo":  round(BOT["lucro_ciclo"], 6),
        "roi":          round(roi, 4),
        "ciclos_jc":    BOT["ciclos_jc"],
        "prog_ciclo":   round(prog, 2),
        "gatilho_usdt": round(g, 6),
        "falta":        round(max(0, g - BOT["lucro_ciclo"]), 6),
        "arbs_exec":    BOT["arbs_exec"],
        "arbs_achadas": BOT["arbs_achadas"],
        "arbs_rejeit":  BOT["arbs_rejeit"],
        "melhor":       round(BOT["melhor"], 4),
        "drawdown":     round(BOT["drawdown"], 2),
        "scans":        BOT["scans"],
        "last_arb":     BOT["last_arb"],
        "marcos":       BOT["marcos"][:5],
        "max_dd":       BOT["max_dd"],
        "gatilho_jc":   BOT["gatilho_jc"],
    })

@app.route("/api/logs")
def api_logs():
    return jsonify(BOT["logs"][:80])

@app.route("/api/scan")
def api_scan():
    return jsonify(BOT["scan_data"][:20])

@app.route("/api/start", methods=["POST"])
def api_start():
    if BOT["running"]:
        return jsonify({"ok": False, "msg": "Ja esta a correr"})
    d = request.json or {}
    with _lock:
        BOT.update({
            "paper":       d.get("paper", True),
            "capital":     float(d.get("capital", 10)),
            "cap_inicial": float(d.get("capital", 10)),
            "cap_base":    float(d.get("capital", 10)),
            "saldo_conta": float(d.get("saldo_conta", d.get("capital", 10))),
            "gatilho_jc":  float(d.get("gatilho_jc", 10)),
            "lucro_min":   float(d.get("lucro_min", 0.20)),
            "slip_max":    float(d.get("slip_max", 0.05)),
            "max_dd":      float(d.get("max_dd", 10)),
            "api_key":     d.get("api_key", BOT["api_key"]),
            "api_secret":  d.get("api_secret", BOT["api_secret"]),
            "lucro_total": 0, "lucro_ciclo": 0, "ciclos_jc": 0,
            "arbs_exec": 0, "arbs_achadas": 0, "arbs_rejeit": 0,
            "scans": 0, "melhor": 0, "drawdown": 0, "arbs_hora": 0,
            "cooldowns": {}, "logs": [], "scan_data": [],
            "last_arb": None, "marcos": [], "running": True,
        })
    threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    BOT["running"] = False
    return jsonify({"ok": True})

@app.route("/api/config", methods=["POST"])
def api_config():
    d = request.json or {}
    for k, v in d.items():
        if k in BOT:
            BOT[k] = v
    return jsonify({"ok": True})

# ═══════════════════════════════════════════
#  DASHBOARD — HTML embutido (sem ficheiros externos)
# ═══════════════════════════════════════════
DASHBOARD = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>ARB Bot</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');

:root {
  --bg:#060910; --s1:#0b0e1a; --s2:#0f1422; --s3:#141c2e; --s4:#1a2438;
  --b:#1f2e48; --b2:#283a58;
  --cy:#00ccff; --gr:#00e09a; --grdk:#00a870;
  --gd:#f0bc10; --rd:#ff3868; --or:#ff7a18; --sk:#3db0ff;
  --tx:#d5e8f8; --t2:#748aaa; --mu:#2c3e58; --mu2:#445e80;
}

*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{
  font-family:'Syne',sans-serif;color:var(--tx);
  font-size:14px;line-height:1.5;
  display:flex;flex-direction:column;
  max-width:480px;margin:0 auto;
}
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 70% 40% at 50% -5%,#00ccff10 0%,transparent 70%),
    linear-gradient(var(--b)10 1px,transparent 1px),
    linear-gradient(90deg,var(--b)10 1px,transparent 1px);
  background-size:100%,42px 42px,42px 42px;
}

/* ── APP SHELL ── */
#app{
  position:relative;z-index:1;
  display:flex;flex-direction:column;
  height:100vh;width:100%;
}

/* ── TOP BAR ── */
#topbar{
  flex-shrink:0;height:52px;
  background:var(--s1)ee;backdrop-filter:blur(24px);
  border-bottom:1px solid var(--b);
  padding:0 16px;
  display:flex;align-items:center;justify-content:space-between;
}
.logo{
  font-family:'Syne',sans-serif;font-weight:800;font-size:18px;letter-spacing:2px;
  background:linear-gradient(135deg,var(--cy),var(--gr));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.logo small{
  font-size:10px;letter-spacing:.5px;font-weight:400;
  -webkit-text-fill-color:var(--mu2);color:var(--mu2);margin-left:4px;
}
#topRight{display:flex;align-items:center;gap:8px}
#topSaldo{
  font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;
  color:var(--cy);opacity:0;transition:opacity .3s;
}
.pill{
  display:flex;align-items:center;gap:5px;
  font-size:10px;font-weight:700;letter-spacing:1px;
  padding:5px 11px;border-radius:20px;transition:all .3s;
}
.pill-off{border:1px solid var(--mu)50;background:var(--mu)15;color:var(--mu2)}
.pill-on{border:1px solid var(--gr)55;background:var(--gr)12;color:var(--gr)}
.pdot{width:6px;height:6px;border-radius:50%;transition:all .3s}
.pdot-off{background:var(--mu2)}
.pdot-on{background:var(--gr);box-shadow:0 0 8px var(--gr);animation:blink 1.4s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}

/* ── SCROLL ── */
#scroll{
  flex:1;overflow-y:auto;overflow-x:hidden;
  -webkit-overflow-scrolling:touch;
}
#scroll::-webkit-scrollbar{width:2px}
#scroll::-webkit-scrollbar-thumb{background:var(--b2)}

/* ── PAGES ── */
.pg{display:none;padding:14px 14px 16px;animation:fu .2s ease}
.pg.show{display:block}
@keyframes fu{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}

/* ── BOTTOM NAV — FIXED ── */
#nav{
  flex-shrink:0;
  background:var(--s1)f0;backdrop-filter:blur(24px);
  border-top:1px solid var(--b);
  display:grid;grid-template-columns:repeat(4,1fr);
  height:56px;position:relative;z-index:50;
}
.nb{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;background:none;border:none;cursor:pointer;outline:none;
  color:var(--mu2);transition:color .2s;border-top:2px solid transparent;
  padding:6px 4px 4px;
}
.nb svg{width:20px;height:20px;stroke-width:1.8;
  fill:none;stroke:currentColor;transition:all .2s}
.nb .lbl{font-size:8px;font-weight:700;letter-spacing:1px;
  text-transform:uppercase;font-family:'Syne',sans-serif}
.nb.act{color:var(--cy);border-top-color:var(--cy)}
.nb.act svg{stroke:var(--cy)}

/* ── CARDS ── */
.card{background:var(--s1);border:1px solid var(--b);border-radius:12px;padding:16px;margin-bottom:10px}
.card-cy{background:linear-gradient(135deg,#00ccff08,var(--s1));border-color:#00ccff28}
.card-gr{background:linear-gradient(135deg,#00e09a08,var(--s1));border-color:#00e09a28}
.card-gd{background:linear-gradient(135deg,#f0bc1008,var(--s1));border-color:#f0bc1028}

/* ── TAGS ── */
.tag{display:inline-flex;align-items:center;gap:4px;font-size:9px;font-weight:700;
  letter-spacing:1px;text-transform:uppercase;padding:3px 8px;border-radius:20px;white-space:nowrap}
.t-cy{background:#00ccff14;color:var(--cy);border:1px solid #00ccff30}
.t-gr{background:#00e09a14;color:var(--gr);border:1px solid #00e09a30}
.t-gd{background:#f0bc1014;color:var(--gd);border:1px solid #f0bc1030}
.t-rd{background:#ff386814;color:var(--rd);border:1px solid #ff386830}
.t-or{background:#ff7a1814;color:var(--or);border:1px solid #ff7a1830}
.t-mu{background:var(--s4);color:var(--mu2);border:1px solid var(--b)}

/* ══ DUAL SALDO (lado a lado) ══ */
#dualSaldo{
  display:grid;grid-template-columns:1fr 1fr;
  gap:0;background:var(--b);border-radius:14px;
  overflow:hidden;border:1px solid var(--b);margin-bottom:10px;
}
.sc{padding:14px 14px;transition:background .4s}
.sc:first-child{background:var(--s2);border-right:1px solid var(--b)}
.sc:last-child{background:var(--s1)}
.sc-lbl{
  font-size:9px;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--mu2);margin-bottom:5px;
  display:flex;align-items:center;gap:4px;
}
.sc-val{
  font-family:'JetBrains Mono',monospace;font-weight:700;
  font-size:17px;line-height:1.15;
  transition:text-shadow .4s,color .3s;
}
.sc-sub{font-size:9px;color:var(--mu2);margin-top:3px}
.ldot{width:5px;height:5px;border-radius:50%;
  background:var(--cy);animation:blink 1.5s infinite;display:none}

/* ── MAIN BTN ── */
#btnMain{
  width:100%;padding:14px;border-radius:11px;border:none;
  cursor:pointer;font-family:'Syne',sans-serif;font-weight:800;
  font-size:14px;letter-spacing:.4px;transition:all .2s;outline:none;margin-bottom:10px;
}
#btnMain:active{transform:scale(.98)}
.btn-go{background:linear-gradient(135deg,var(--gr),var(--grdk));
  color:#000;box-shadow:0 4px 24px #00e09a30}
.btn-stop{background:linear-gradient(135deg,var(--rd),#cc0040);
  color:#fff;box-shadow:0 4px 24px #ff386830}

/* ── ARB COUNTER ── */
#arbGrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}
.ab{border-radius:11px;padding:13px 10px;text-align:center}
.ab.main{background:linear-gradient(135deg,#00e09a14,var(--s3));border:1px solid #00e09a32}
.ab.side{background:var(--s3);border:1px solid var(--b)}
.an{font-family:'JetBrains Mono',monospace;font-weight:800;line-height:1;transition:text-shadow .4s}
.al{font-size:8px;letter-spacing:1px;text-transform:uppercase;margin-top:5px;font-weight:700}

/* ── 2x2 METRICS ── */
.m2g{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.mc{background:var(--s2);border:1px solid var(--b);border-radius:11px;padding:13px 14px}
.mv{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:18px;line-height:1.15}
.ml{font-size:9px;color:var(--mu2);letter-spacing:1.2px;text-transform:uppercase;margin-top:4px}

/* ── JC RING ── */
#jcCard{
  background:linear-gradient(135deg,#f0bc1009,var(--s2));
  border:1px solid #f0bc1030;border-radius:12px;
  padding:16px;margin-bottom:10px;
}
#jcRow{display:flex;align-items:center;gap:14px;margin-bottom:12px}
.jci{font-size:12px;color:var(--t2);line-height:2.1;flex:1}
.jkv{display:flex;justify-content:space-between;align-items:center}
.jkv strong{font-family:'JetBrains Mono',monospace;font-size:12px}
#progBar{height:6px;background:var(--s4);border-radius:3px;overflow:hidden;margin-bottom:3px}
#progFill{height:100%;border-radius:3px;
  background:linear-gradient(90deg,var(--gd),var(--or));
  transition:width .6s cubic-bezier(.4,0,.2,1)}
.pe{display:flex;justify-content:space-between;margin-top:2px}
.pe span{font-size:9px;font-family:'JetBrains Mono',monospace}

/* ── LAST ARB ── */
#laCard{
  border-radius:12px;padding:14px 16px;margin-bottom:10px;display:none;
  border:1px solid #00e09a28;
  background:linear-gradient(135deg,#00e09a0c,var(--s2));
  transition:border-color .4s;
}
#laCard.fl{border-color:#00e09a70}
.la-hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.la-path{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;
  color:var(--tx);margin-bottom:9px;word-break:break-all;line-height:1.4}
.la-g{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.la-b{background:var(--s3);border-radius:8px;padding:8px;text-align:center}
.la-v{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700}
.la-l{font-size:8px;color:var(--mu2);margin-top:2px;text-transform:uppercase;letter-spacing:1px}

/* ── SCAN ── */
#scanTbl{background:var(--s1);border:1px solid var(--b);border-radius:12px;overflow:hidden;margin-bottom:10px}
.th{display:grid;grid-template-columns:1fr 68px 58px 30px;
  padding:9px 12px;background:var(--s3);border-bottom:1px solid var(--b)}
.th span{font-size:8px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--mu2)}
.th span:not(:first-child){text-align:right}
.tr{display:grid;grid-template-columns:1fr 68px 58px 30px;
  padding:9px 12px;border-bottom:1px solid var(--b)15;align-items:center}
.tr:last-child{border:none}
.tr.ok{background:#00e09a06}
.tc{font-family:'JetBrains Mono',monospace;font-size:10.5px}
.tc:not(:first-child){text-align:right}

/* ── LOG ── */
#logList{background:var(--s1);border:1px solid var(--b);border-radius:12px;
  max-height:calc(100vh - 180px);overflow-y:auto}
#logList::-webkit-scrollbar{width:2px}
#logList::-webkit-scrollbar-thumb{background:var(--b2)}
.lr{padding:7px 13px;border-bottom:1px solid var(--b)18;
  font-family:'JetBrains Mono',monospace;font-size:10.5px;line-height:1.5}
.lr:last-child{border:none}
.lt{color:var(--mu2);margin-right:7px}

/* ── CONFIG ── */
.cs{background:var(--s2);border:1px solid var(--b);border-radius:12px;padding:16px;margin-bottom:10px}
.fl{font-size:9px;color:var(--mu2);letter-spacing:1.2px;text-transform:uppercase;margin-bottom:6px;display:block}
.inp{background:var(--s4);border:1px solid var(--b2);border-radius:8px;
  padding:11px 14px;color:var(--tx);font-size:14px;width:100%;
  outline:none;font-family:'JetBrains Mono',monospace;font-weight:700;
  transition:border-color .2s}
.inp:focus{border-color:var(--cy)60}
.fr{margin-bottom:12px}
.ig{display:flex;gap:8px;align-items:center}
.qb{padding:7px 12px;border-radius:8px;cursor:pointer;font-weight:700;font-size:11px;
  font-family:'JetBrains Mono',monospace;background:var(--s4);border:1px solid var(--b);
  color:var(--mu2);transition:all .15s;flex-shrink:0}
.qb:active{transform:scale(.96)}

/* ── TOGGLE ── */
.trow{display:flex;justify-content:space-between;align-items:center;
  padding:12px 0;border-bottom:1px solid var(--b)25}
.trow:last-child{border:none}
.trow h4{font-size:13px;font-weight:700;margin-bottom:2px}
.trow p{font-size:11px;color:var(--mu2)}
.tgl{width:48px;height:27px;border-radius:14px;position:relative;
  cursor:pointer;border:none;outline:none;flex-shrink:0;transition:background .25s}
.tk{position:absolute;top:3px;width:21px;height:21px;border-radius:50%;
  background:#fff;transition:left .25s;box-shadow:0 1px 6px rgba(0,0,0,.5)}

/* ── UTIL ── */
.sh{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.sh h2{font-size:16px;font-weight:800}
.mini{background:var(--s3);border:1px solid var(--b);color:var(--t2);
  padding:6px 12px;border-radius:7px;font-size:10px;font-weight:700;
  cursor:pointer;font-family:'Syne',sans-serif;letter-spacing:.5px}
.btn-save{width:100%;padding:14px;border-radius:11px;border:none;
  cursor:pointer;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;
  background:linear-gradient(135deg,var(--gr),var(--grdk));
  color:#000;box-shadow:0 4px 20px #00e09a25;margin-bottom:10px}
.btn-save:active{transform:scale(.98)}
.info-box{background:#00ccff08;border:1px solid #00ccff25;border-radius:9px;
  padding:10px 13px;font-size:12px;color:var(--t2);margin-top:8px;line-height:1.6}
.warn-box{background:#ff386810;border:1px solid #ff386830;border-radius:9px;
  padding:10px 13px;font-size:12px;color:var(--rd);margin-top:10px;line-height:1.6}
#modeLine{text-align:center;padding:6px 0 2px}
.empty{padding:44px 16px;text-align:center;color:var(--mu2)}
.empty-ic{font-size:40px;margin-bottom:10px}
.empty h3{font-size:14px;font-weight:700;color:var(--t2);margin-bottom:6px}
.empty p{font-size:12px;line-height:1.6}
</style>
</head>
<body>
<div id="app">

<!-- TOP BAR -->
<div id="topbar">
  <div class="logo">ARB △<small>TRIANGULAR</small></div>
  <div id="topRight">
    <span id="topSaldo"></span>
    <div class="pill pill-off" id="pill">
      <div class="pdot pdot-off" id="pdot"></div>
      <span id="ptxt">OFFLINE</span>
    </div>
  </div>
</div>

<!-- SCROLL AREA -->
<div id="scroll">

<!-- ══ DASHBOARD ══ -->
<div class="pg show" id="pgDash">

  <!-- DUAL SALDO LADO A LADO -->
  <div id="dualSaldo">
    <div class="sc" id="scConta">
      <div class="sc-lbl">
        <span class="ldot" id="ldot"></span>
        Conta Binance
        <span class="tag t-cy" id="liveTag" style="display:none;font-size:8px">LIVE</span>
      </div>
      <div class="sc-val" id="vConta" style="color:var(--cy)">—</div>
      <div class="sc-sub" id="subConta">aguardando</div>
    </div>
    <div class="sc" id="scBot">
      <div class="sc-lbl">
        <span id="bdot" style="width:5px;height:5px;border-radius:50%;
          background:var(--mu2);display:inline-block;transition:all .3s"></span>
        Capital Bot
      </div>
      <div class="sc-val" id="vBot" style="color:var(--gr)">—</div>
      <div class="sc-sub" id="subBot">após reinvestimento</div>
    </div>
  </div>

  <!-- START/STOP -->
  <button id="btnMain" class="btn-go" onclick="toggleBot()">▶&nbsp; Iniciar Bot</button>

  <!-- ARB COUNTER -->
  <div id="arbGrid">
    <div class="ab main" id="boxE">
      <div class="an" id="nE" style="font-size:36px;color:var(--gr)">0</div>
      <div class="al" style="color:var(--gr)">Executadas</div>
    </div>
    <div class="ab side">
      <div class="an" id="nA" style="font-size:24px;color:var(--sk)">0</div>
      <div class="al" style="color:var(--mu2)">Achadas</div>
    </div>
    <div class="ab side">
      <div class="an" id="nR" style="font-size:24px;color:var(--mu2)">0</div>
      <div class="al" style="color:var(--mu2)">Rejeitadas</div>
    </div>
  </div>

  <!-- METRICS -->
  <div class="m2g">
    <div class="mc" style="border-top:2px solid var(--gr)">
      <div class="mv" id="mL" style="color:var(--gr)">+$0.000000</div>
      <div class="ml">Lucro Total</div>
    </div>
    <div class="mc" style="border-top:2px solid var(--gd)">
      <div class="mv" id="mS" style="color:var(--gd)">0.0000%</div>
      <div class="ml">Melhor Spread</div>
    </div>
    <div class="mc" style="border-top:2px solid var(--gd)">
      <div class="mv" id="mC" style="color:var(--gd)">0</div>
      <div class="ml">Ciclos JC</div>
    </div>
    <div class="mc" style="border-top:2px solid var(--mu2)">
      <div class="mv" id="mD" style="color:var(--mu2)">0.00%</div>
      <div class="ml">Drawdown</div>
    </div>
  </div>

  <!-- JC RING -->
  <div id="jcCard">
    <div id="jcRow">
      <div style="position:relative;width:82px;height:82px;flex-shrink:0">
        <svg width="82" height="82" style="transform:rotate(-90deg)">
          <circle cx="41" cy="41" r="34" fill="none" stroke="var(--s4)" stroke-width="7"/>
          <circle id="ringC" cx="41" cy="41" r="34" fill="none" stroke="var(--gd)"
            stroke-width="7" stroke-dasharray="0 214" stroke-linecap="round"
            style="transition:stroke-dasharray .5s cubic-bezier(.4,0,.2,1)"/>
        </svg>
        <div style="position:absolute;inset:0;display:flex;flex-direction:column;
          align-items:center;justify-content:center;gap:1px;text-align:center">
          <div id="rPct" style="font-family:'JetBrains Mono',monospace;font-size:15px;
            font-weight:700;color:var(--gd)">0%</div>
          <div style="font-size:7px;color:var(--mu2);letter-spacing:1px">CICLO</div>
        </div>
      </div>
      <div style="flex:1">
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
          <span class="tag t-gd">🔁 Juros Compostos</span>
          <span class="tag t-gr" id="jcTag" style="font-size:8px">#0</span>
        </div>
        <div class="jci">
          <div class="jkv"><span>Lucro ciclo</span>
            <strong id="jL" style="color:var(--gd)">$0.000000</strong></div>
          <div class="jkv"><span>Gatilho</span>
            <strong id="jG" style="color:var(--tx)">$0.0000</strong></div>
          <div class="jkv"><span>Falta</span>
            <strong id="jF" style="color:var(--t2)">$0.000000</strong></div>
        </div>
      </div>
    </div>
    <div id="progBar"><div id="progFill" style="width:0%"></div></div>
    <div class="pe">
      <span style="color:var(--mu)">$0</span>
      <span id="pPct" style="color:var(--gd);font-weight:700">0%</span>
      <span id="pMax" style="color:var(--mu)">$0</span>
    </div>
  </div>

  <!-- LAST ARB -->
  <div id="laCard">
    <div class="la-hd">
      <span class="tag t-gr" id="laTag">✨ Arb #0</span>
      <span id="laLucro" style="font-family:'JetBrains Mono',monospace;
        color:var(--gr);font-weight:700;font-size:14px">+$0</span>
    </div>
    <div class="la-path" id="laPath">—</div>
    <div class="la-g">
      <div class="la-b"><div class="la-v" id="laSpd" style="color:var(--gd)">0%</div><div class="la-l">Spread</div></div>
      <div class="la-b"><div class="la-v" id="laSlp" style="color:var(--or)">0%</div><div class="la-l">Slippage</div></div>
      <div class="la-b"><div class="la-v" id="laLiq" style="color:var(--sk)">$0</div><div class="la-l">Liquidez</div></div>
    </div>
  </div>

  <div id="modeLine"></div>
</div>

<!-- ══ SCAN ══ -->
<div class="pg" id="pgScan">
  <div class="sh">
    <h2>Scan ao Vivo</h2>
    <div style="display:flex;gap:8px;align-items:center">
      <span id="scCount" style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--mu2)">0 scans</span>
      <span class="tag t-gr" id="scLive" style="display:none;font-size:8px">● LIVE</span>
    </div>
  </div>
  <div id="scanTbl">
    <div class="th">
      <span>Triângulo</span><span>Spread</span><span>Slip</span><span></span>
    </div>
    <div id="scanBody">
      <div class="empty">
        <div class="empty-ic">△</div>
        <h3>Bot parado</h3>
        <p>Inicia o bot para ver os triângulos em tempo real.</p>
      </div>
    </div>
  </div>
</div>

<!-- ══ LOGS ══ -->
<div class="pg" id="pgLogs">
  <div class="sh">
    <h2>Registo</h2>
    <button class="mini" onclick="document.getElementById('logList').innerHTML=
      '<div class=empty><div class=empty-ic>≡</div><h3>Limpo</h3></div>'">Limpar</button>
  </div>
  <div id="logList">
    <div class="empty"><div class="empty-ic">≡</div><h3>Sem registos</h3></div>
  </div>
</div>

<!-- ══ CONFIG ══ -->
<div class="pg" id="pgConfig">
  <div class="sh"><h2>Configurações</h2></div>

  <!-- API KEYS -->
  <div class="cs">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <span class="tag t-cy">🔑 Chaves API Binance</span>
    </div>
    <div class="fr">
      <label class="fl">API Key</label>
      <input class="inp" type="text" id="cfgK"
        placeholder="Cole a tua API Key aqui"
        autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
    </div>
    <div class="fr">
      <label class="fl">API Secret</label>
      <input class="inp" type="password" id="cfgS"
        placeholder="Cole o teu Secret Key aqui"
        autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
      <button onclick="var e=document.getElementById('cfgS');e.type=e.type=='password'?'text':'password'"
        style="margin-top:6px;background:var(--s4);border:1px solid var(--b);
        color:var(--mu2);padding:5px 10px;border-radius:6px;font-size:10px;cursor:pointer">
        👁 Mostrar / Ocultar
      </button>
    </div>
    <div class="info-box">
      🔒 Na Binance activa <strong style="color:var(--gr)">Spot Trading</strong> apenas —
      <strong style="color:var(--rd)">NUNCA Withdrawals</strong>
    </div>
  </div>

  <!-- CAPITAL -->
  <div class="cs">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <span class="tag t-cy">💰 Capital</span>
    </div>
    <div class="fr">
      <label class="fl">Capital operacional do bot (USDT)</label>
      <div class="ig">
        <button class="qb" onclick="document.getElementById('cfgCap').value=10;upG()"
          style="background:#00ccff10;border-color:#00ccff30;color:var(--cy)">
          MIN<br><small>$10</small>
        </button>
        <input class="inp" type="number" id="cfgCap" value="10"
          min="10" step="1" oninput="upG()"
          style="flex:1;text-align:right;font-size:16px">
        <button class="qb" id="btnMx"
          onclick="document.getElementById('cfgCap').value=document.getElementById('cfgConta').value;upG()"
          style="background:#00e09a10;border-color:#00e09a30;color:var(--gr)">
          MÁX<br><small id="mxVal">$?</small>
        </button>
      </div>
    </div>
    <div class="fr">
      <label class="fl">Saldo total da conta Binance (USDT)</label>
      <input class="inp" type="number" id="cfgConta" value="10" min="0" step="1"
        oninput="document.getElementById('mxVal').textContent='$'+this.value;upG()">
    </div>
  </div>

  <!-- MODO SIMULAÇÃO / REAL -->
  <div class="cs">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <span class="tag t-gr">⚙️ Modo de Operação</span>
    </div>
    <div class="trow">
      <div>
        <h4>Simulação (Paper Trading)</h4>
        <p>Dados reais Binance · sem ordens reais</p>
      </div>
      <button class="tgl" id="tglP" onclick="tgPaper()" style="background:var(--gr)">
        <div class="tk" id="tgPK" style="left:24px"></div>
      </button>
    </div>
    <div id="wReal" style="display:none" class="warn-box">
      ⚠️ <strong>Modo REAL</strong> — o bot vai executar ordens reais na Binance!
    </div>
  </div>

  <!-- JUROS COMPOSTOS -->
  <div class="cs">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <span class="tag t-gd">🔁 Juros Compostos</span>
    </div>
    <div class="fr">
      <label class="fl">Gatilho de reinvestimento</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px" id="gBtns">
        <button class="qb" onclick="sG(this,5)"  data-v="5">5%</button>
        <button class="qb act-gd" onclick="sG(this,10)" data-v="10">10%</button>
        <button class="qb" onclick="sG(this,15)" data-v="15">15%</button>
        <button class="qb" onclick="sG(this,20)" data-v="20">20%</button>
        <button class="qb" onclick="sG(this,25)" data-v="25">25%</button>
      </div>
      <div class="info-box" id="gInfo">
        Reinveste quando lucrar <strong style="color:var(--gd);
        font-family:'JetBrains Mono',monospace">$1.0000 USDT</strong>
      </div>
    </div>
  </div>

  <!-- PARÂMETROS -->
  <div class="cs">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <span class="tag t-or">⚡ Parâmetros</span>
    </div>
    <div class="fr">
      <label class="fl">Lucro mínimo por arb</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="lBtns">
        <button class="qb" onclick="sL(this,0.10)" data-v="0.10">0.10%</button>
        <button class="qb" onclick="sL(this,0.15)" data-v="0.15">0.15%</button>
        <button class="qb act-or" onclick="sL(this,0.20)" data-v="0.20">0.20%</button>
        <button class="qb" onclick="sL(this,0.30)" data-v="0.30">0.30%</button>
        <button class="qb" onclick="sL(this,0.50)" data-v="0.50">0.50%</button>
      </div>
    </div>
    <div class="fr" style="margin-top:10px">
      <label class="fl">Max Drawdown (para o bot automaticamente)</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="dBtns">
        <button class="qb" onclick="sD(this,5)"  data-v="5">5%</button>
        <button class="qb act-rd" onclick="sD(this,10)" data-v="10">10%</button>
        <button class="qb" onclick="sD(this,15)" data-v="15">15%</button>
        <button class="qb" onclick="sD(this,20)" data-v="20">20%</button>
        <button class="qb" onclick="sD(this,30)" data-v="30">30%</button>
      </div>
    </div>
  </div>

  <button class="btn-save" onclick="saveAll()">✓ Guardar e Aplicar</button>
  <div style="height:16px"></div>
</div>

</div><!-- /scroll -->

<!-- BOTTOM NAV — SEMPRE VISÍVEL -->
<nav id="nav">
  <button class="nb act" id="nbD" onclick="goPage('Dash',this)">
    <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
    <span class="lbl">Dashboard</span>
  </button>
  <button class="nb" id="nbS" onclick="goPage('Scan',this)">
    <svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
    <span class="lbl">Scan</span>
  </button>
  <button class="nb" id="nbL" onclick="goPage('Logs',this)">
    <svg viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="3" cy="6" r="1" fill="currentColor"/><circle cx="3" cy="12" r="1" fill="currentColor"/><circle cx="3" cy="18" r="1" fill="currentColor"/></svg>
    <span class="lbl">Logs</span>
  </button>
  <button class="nb" id="nbC" onclick="goPage('Config',this)">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    <span class="lbl">Config</span>
  </button>
</nav>

</div><!-- /app -->

<style>
.act-gd{background:#f0bc1018!important;border-color:#f0bc1050!important;color:var(--gd)!important}
.act-or{background:#ff7a1818!important;border-color:#ff7a1850!important;color:var(--or)!important}
.act-rd{background:#ff386818!important;border-color:#ff386850!important;color:var(--rd)!important}
</style>

<script>
// ── STATE ──────────────────────────────────────────
const S={paper:true,gatilho:10,lmin:0.20,dd:10,prevExec:0,prevCiclos:0};

// ── NAVIGATION ─────────────────────────────────────
function goPage(id,btn){
  document.querySelectorAll('.pg').forEach(p=>p.classList.remove('show'));
  document.querySelectorAll('.nb').forEach(b=>b.classList.remove('act'));
  document.getElementById('pg'+id).classList.add('show');
  btn.classList.add('act');
  document.getElementById('scroll').scrollTop=0;
}

// ── CONFIG ─────────────────────────────────────────
function upG(){
  const cap=parseFloat(document.getElementById('cfgCap').value)||10;
  const g=(cap*S.gatilho/100).toFixed(4);
  document.getElementById('gInfo').innerHTML=
    `Reinveste quando lucrar <strong style="color:var(--gd);font-family:'JetBrains Mono',monospace">$${g} USDT</strong>`;
}
function tgPaper(){
  S.paper=!S.paper;
  document.getElementById('tglP').style.background=S.paper?'var(--gr)':'var(--mu)';
  document.getElementById('tgPK').style.left=S.paper?'24px':'3px';
  document.getElementById('wReal').style.display=S.paper?'none':'block';
}
function sG(btn,v){
  S.gatilho=v;
  document.querySelectorAll('#gBtns .qb').forEach(b=>b.classList.remove('act-gd'));
  btn.classList.add('act-gd');
  upG();
}
function sL(btn,v){
  S.lmin=v;
  document.querySelectorAll('#lBtns .qb').forEach(b=>b.classList.remove('act-or'));
  btn.classList.add('act-or');
}
function sD(btn,v){
  S.dd=v;
  document.querySelectorAll('#dBtns .qb').forEach(b=>b.classList.remove('act-rd'));
  btn.classList.add('act-rd');
}
function saveAll(){
  const cfg={
    api_key:document.getElementById('cfgK').value.trim(),
    api_secret:document.getElementById('cfgS').value.trim(),
    paper:S.paper, gatilho_jc:S.gatilho, lucro_min:S.lmin, max_dd:S.dd,
  };
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  goPage('Dash',document.getElementById('nbD'));
}

// ── BOT ────────────────────────────────────────────
function toggleBot(){
  if(window._running){
    fetch('/api/stop',{method:'POST'});
  } else {
    const p={
      paper:S.paper,
      capital:parseFloat(document.getElementById('cfgCap').value)||10,
      saldo_conta:parseFloat(document.getElementById('cfgConta').value)||10,
      gatilho_jc:S.gatilho, lucro_min:S.lmin, slip_max:0.05, max_dd:S.dd,
      api_key:document.getElementById('cfgK').value.trim(),
      api_secret:document.getElementById('cfgS').value.trim(),
    };
    fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});
  }
}
window._running=false;

// ── POLL ───────────────────────────────────────────
function poll(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    window._running=d.running;

    // Top bar
    const btn=document.getElementById('btnMain');
    const pill=document.getElementById('pill');
    const pdot=document.getElementById('pdot');
    btn.textContent=d.running?'⏹  Parar Bot':'▶  Iniciar Bot';
    btn.className=d.running?'btn-stop':'btn-go';
    pill.className=d.running?'pill pill-on':'pill pill-off';
    pdot.className=d.running?'pdot pdot-on':'pdot pdot-off';
    document.getElementById('ptxt').textContent=d.running?'ONLINE':'OFFLINE';

    // Top saldo mini
    const ts=document.getElementById('topSaldo');
    ts.textContent=d.running?`$${d.saldo_conta.toFixed(4)}`:'';
    ts.style.opacity=d.running?'1':'0';

    // Live indicators
    document.getElementById('ldot').style.display=d.running?'inline-block':'none';
    document.getElementById('liveTag').style.display=d.running?'inline-flex':'none';
    document.getElementById('scLive').style.display=d.running?'inline-flex':'none';

    // DUAL SALDO
    document.getElementById('vConta').textContent=d.running?`$${d.saldo_conta.toFixed(4)}`:'—';
    document.getElementById('subConta').textContent=d.paper?'simulado · actualiza a cada arb':'saldo real · actualiza a cada arb';
    const roi=d.roi;
    document.getElementById('vBot').textContent=d.running?`$${d.capital.toFixed(6)}`:'—';
    document.getElementById('vBot').style.color=roi>=0?'var(--gr)':'var(--rd)';
    document.getElementById('subBot').textContent=d.running
      ?(roi>=0?'▲':'▼')+' '+Math.abs(roi).toFixed(4)+'% ROI · após reinvestimento'
      :'após reinvestimento';

    // Flash conta (arb executada)
    if(d.arbs_exec>S.prevExec){
      S.prevExec=d.arbs_exec;
      const cell=document.getElementById('scConta');
      const nE=document.getElementById('nE');
      const la=document.getElementById('laCard');
      cell.style.background='linear-gradient(135deg,#00ccff14,var(--s2))';
      nE.style.textShadow='0 0 20px var(--gr)';
      la.classList.add('fl');
      setTimeout(()=>{
        cell.style.background='';
        nE.style.textShadow='';
        la.classList.remove('fl');
      },700);
    }

    // Flash bot (juros compostos)
    if(d.ciclos_jc>S.prevCiclos){
      S.prevCiclos=d.ciclos_jc;
      const bc=document.getElementById('scBot');
      const vb=document.getElementById('vBot');
      const bd=document.getElementById('bdot');
      bc.style.background='linear-gradient(135deg,#00e09a14,var(--s1))';
      vb.style.textShadow='0 0 20px var(--gr)';
      bd.style.background='var(--gr)';
      bd.style.boxShadow='0 0 8px var(--gr)';
      setTimeout(()=>{
        bc.style.background='';
        vb.style.textShadow='';
        bd.style.background='var(--mu2)';
        bd.style.boxShadow='';
      },1400);
    }

    // Counters
    document.getElementById('nE').textContent=d.arbs_exec;
    document.getElementById('nA').textContent=d.arbs_achadas;
    document.getElementById('nR').textContent=d.arbs_rejeit;

    // Metrics
    document.getElementById('mL').textContent=`+$${d.lucro_total.toFixed(6)}`;
    document.getElementById('mS').textContent=`${d.melhor.toFixed(4)}%`;
    document.getElementById('mC').textContent=d.ciclos_jc;
    const dd=document.getElementById('mD');
    dd.textContent=`${d.drawdown.toFixed(2)}%`;
    dd.style.color=d.drawdown>d.max_dd*.7?'var(--rd)':d.drawdown>d.max_dd*.4?'var(--or)':'var(--mu2)';

    // JC Ring
    const circ=2*Math.PI*34,prog=d.prog_ciclo;
    document.getElementById('ringC').setAttribute('stroke-dasharray',`${circ*prog/100} ${circ}`);
    document.getElementById('rPct').textContent=`${prog.toFixed(0)}%`;
    document.getElementById('jcTag').textContent=`#${d.ciclos_jc}`;
    document.getElementById('progFill').style.width=`${prog}%`;
    document.getElementById('pPct').textContent=`${prog.toFixed(1)}%`;
    document.getElementById('pMax').textContent=`$${d.gatilho_usdt.toFixed(4)}`;
    document.getElementById('jL').textContent=`$${d.lucro_ciclo.toFixed(6)}`;
    document.getElementById('jG').textContent=`$${d.gatilho_usdt.toFixed(4)}`;
    const jf=document.getElementById('jF');
    jf.textContent=`$${d.falta.toFixed(6)}`;
    jf.style.color=d.falta<d.gatilho_usdt*.15?'var(--gr)':'var(--t2)';

    // Last arb
    if(d.last_arb){
      const la=d.last_arb;
      document.getElementById('laCard').style.display='block';
      document.getElementById('laTag').textContent=`✨ Arb #${d.arbs_exec}`;
      document.getElementById('laLucro').textContent=`+$${la.lucro.toFixed(6)}`;
      document.getElementById('laPath').textContent=la.tri.replace(/@/g,'→');
      document.getElementById('laSpd').textContent=`${la.pct.toFixed(4)}%`;
      document.getElementById('laSlp').textContent=`${(la.slip||0).toFixed(4)}%`;
      document.getElementById('laLiq').textContent=`$${Math.floor(la.lmin)}`;
    }

    // Mode
    const ml=document.getElementById('modeLine');
    ml.innerHTML=d.running
      ?(d.paper
        ?'<span class="tag t-or">🧪 Simulação · Dados Reais Binance</span>'
        :'<span class="tag t-rd">🔴 Modo Real · Ordens Reais</span>')
      :'';

    document.getElementById('scCount').textContent=`${d.scans} scans`;

  }).catch(()=>{});

  // Scan
  fetch('/api/scan').then(r=>r.json()).then(data=>{
    if(!data||!data.length)return;
    const html=data.map(r=>{
      const col=r.pct>=0.20?'var(--gr)':r.pct>0?'var(--gd)':'var(--rd)';
      const tri=r.tri.replace(/@/g,'→').split('→').slice(1,3).join('→');
      const ok=r.ok?`<span style="color:var(--gr);font-weight:800;font-size:14px">✓</span>`:`<span style="color:var(--mu)">–</span>`;
      return `<div class="tr ${r.ok?'ok':''}">
        <span class="tc" style="color:var(--tx)">${tri}</span>
        <span class="tc" style="color:${col};font-weight:700">${r.pct>=0?'+':''}${r.pct.toFixed(3)}%</span>
        <span class="tc" style="color:var(--mu2)">${(r.slip||0).toFixed(3)}%</span>
        <span class="tc">${ok}</span>
      </div>`;
    }).join('');
    document.getElementById('scanBody').innerHTML=html;
  }).catch(()=>{});

  // Logs
  fetch('/api/logs').then(r=>r.json()).then(logs=>{
    if(!logs||!logs.length)return;
    const colors={success:'var(--gr)',compound:'var(--gd)',warn:'var(--or)',error:'var(--rd)',info:'var(--mu2)'};
    const bgs={success:'#00e09a05',compound:'#f0bc1008',error:'#ff386805'};
    const html=logs.map(l=>`<div class="lr" style="color:${colors[l.t]||colors.info};background:${bgs[l.t]||'transparent'}"><span class="lt">${l.ts}</span>${l.msg}</div>`).join('');
    document.getElementById('logList').innerHTML=html||'<div class="empty"><div class="empty-ic">≡</div><h3>Sem registos</h3></div>';
  }).catch(()=>{});
}

// ── INIT ───────────────────────────────────────────
upG();
poll();
setInterval(poll,2000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return DASHBOARD

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
