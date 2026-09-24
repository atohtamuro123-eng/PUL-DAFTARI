"""
Pul Daftari — Telegram Mini App backend.

Foydalanuvchi hisobi Telegram akkauntiga bog'langan: bot ichida ochilganda
Telegram o'zi kimligini tasdiqlaydi (initData orqali), alohida login kerak
emas. Har bir foydalanuvchining yozuvlari uning Telegram user_id'i bilan
saqlanadi (SQLite fayl bazasida).

O'RNATISH:
    pip install -r requirements.txt

ISHGA TUSHIRISH (lokal sinov uchun):
    BOT_TOKEN=123456:ABC... uvicorn app:app --reload

DEPLOY (Render.com bepul tarifda):
    Build command:  pip install -r requirements.txt
    Start command:  uvicorn app:app --host 0.0.0.0 --port $PORT
    Environment:    BOT_TOKEN = @BotFather bergan tokeningiz
"""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "pul_daftari.db")
INIT_DATA_MAX_AGE = 24 * 60 * 60  # 24 soat

app = FastAPI()


# ---------------------------------------------------------------- storage --
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )"""
        )


init_db()


def load_user(user_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT data FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return json.loads(row["data"])
    return {"transactions": [], "autoNotify": False}


def save_user(user_id: str, data: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, data) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (user_id, json.dumps(data)),
        )


# ------------------------------------------------------- telegram auth -----
def validate_init_data(init_data: str) -> dict:
    """Telegram hujjatlashtirilgan usul bo'yicha initData imzosini tekshiradi.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    if not BOT_TOKEN:
        raise HTTPException(500, "Server BOT_TOKEN sozlanmagan")
    if not init_data:
        raise HTTPException(401, "initData yo'q")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Imzo topilmadi")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(401, "Imzo mos kelmadi — ishonchsiz so'rov")

    auth_date = int(parsed.get("auth_date", "0"))
    if time.time() - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(401, "Sessiya eskirgan, botni qayta oching")

    user = json.loads(parsed.get("user", "{}"))
    if "id" not in user:
        raise HTTPException(401, "Foydalanuvchi aniqlanmadi")

    return user


def get_user(x_telegram_init_data: str = Header(default="")) -> dict:
    return validate_init_data(x_telegram_init_data)


# ------------------------------------------------------------- api models --
class Transaction(BaseModel):
    type: str  # "in" | "out"
    amount: float
    date: str
    desc: str
    category: str


class Settings(BaseModel):
    autoNotify: bool


async def notify_telegram(chat_id: int, text: str):
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )


def fmt(n: float) -> str:
    return f"{round(n):,}".replace(",", " ") + " so'm"


# ---------------------------------------------------------------- routes --
@app.get("/api/state")
def api_get_state(x_telegram_init_data: str = Header(default="")):
    user = validate_init_data(x_telegram_init_data)
    return load_user(str(user["id"]))


@app.post("/api/transactions")
async def api_add_transaction(
    tx: Transaction, x_telegram_init_data: str = Header(default="")
):
    user = validate_init_data(x_telegram_init_data)
    uid = str(user["id"])
    data = load_user(uid)
    new_tx = tx.dict()
    new_tx["id"] = int(time.time() * 1000)
    data["transactions"].append(new_tx)
    save_user(uid, data)

    if data.get("autoNotify"):
        icon = "🟢 Kirim" if tx.type == "in" else "🔴 Chiqim"
        text = f"<b>{icon}</b>\n{fmt(tx.amount)} — {tx.desc}\nKategoriya: {tx.category}\nSana: {tx.date}"
        await notify_telegram(user["id"], text)

    return data


@app.delete("/api/transactions/{tx_id}")
def api_delete_transaction(tx_id: int, x_telegram_init_data: str = Header(default="")):
    user = validate_init_data(x_telegram_init_data)
    uid = str(user["id"])
    data = load_user(uid)
    data["transactions"] = [t for t in data["transactions"] if t["id"] != tx_id]
    save_user(uid, data)
    return data


@app.post("/api/settings")
def api_settings(settings: Settings, x_telegram_init_data: str = Header(default="")):
    user = validate_init_data(x_telegram_init_data)
    uid = str(user["id"])
    data = load_user(uid)
    data["autoNotify"] = settings.autoNotify
    save_user(uid, data)
    return data


@app.post("/api/report")
async def api_report(x_telegram_init_data: str = Header(default="")):
    user = validate_init_data(x_telegram_init_data)
    uid = str(user["id"])
    data = load_user(uid)
    total_in = sum(t["amount"] for t in data["transactions"] if t["type"] == "in")
    total_out = sum(t["amount"] for t in data["transactions"] if t["type"] == "out")
    text = (
        f"<b>📒 Pul Daftari — umumiy hisobot</b>\n"
        f"Jami kirim: {fmt(total_in)}\n"
        f"Jami chiqim: {fmt(total_out)}\n"
        f"Qoldiq: {fmt(total_in - total_out)}\n"
        f"Yozuvlar soni: {len(data['transactions'])}"
    )
    await notify_telegram(user["id"], text)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Pul Daftari</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{
    --bg:#121316; --bg-elev:#1a1c20; --bg-elev-2:#212327; --border:#2a2c31;
    --text:#e7e6e3; --text-dim:#8d8f96; --text-faint:#5c5e64;
    --green:#5fb489; --green-bg:#1a2621; --red:#d17d7d; --red-bg:#2a1c1d;
    --accent:#c9a24d; --font:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{margin:0;background:var(--bg);font-family:var(--font);color:var(--text);
       padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);}
  .wrap{max-width:640px;margin:0 auto;padding:24px 20px 60px;}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;}
  header h1{font-size:19px;font-weight:600;margin:0;}
  header .subtitle{font-size:12.5px;color:var(--text-dim);margin-top:3px;}
  .sync-status{font-size:11.5px;color:var(--green);margin-bottom:22px;display:flex;align-items:center;gap:6px;}
  .sync-status .sdot{width:6px;height:6px;border-radius:50%;background:var(--green);}
  .balance-card{background:var(--bg-elev);border:1px solid var(--border);border-radius:14px;padding:22px;margin-bottom:14px;}
  .balance-card .label{font-size:12px;color:var(--text-dim);}
  .balance-card .amount{font-size:34px;font-weight:700;margin-top:6px;}
  .balance-card.negative .amount{color:var(--red);}
  .mini-stats{display:flex;gap:12px;margin-bottom:24px;}
  .mini-stats .card{flex:1;background:var(--bg-elev);border:1px solid var(--border);border-radius:12px;padding:14px 16px;}
  .mini-stats .card .k{font-size:11.5px;color:var(--text-dim);}
  .mini-stats .card .v{font-size:18px;font-weight:600;margin-top:4px;}
  .mini-stats .in .v{color:var(--green);}
  .mini-stats .out .v{color:var(--red);}
  .tabs{display:flex;gap:2px;margin-bottom:22px;border-bottom:1px solid var(--border);}
  .tab{padding:10px 4px;margin-right:22px;cursor:pointer;font-size:13.5px;color:var(--text-dim);border-bottom:2px solid transparent;}
  .tab.active{color:var(--text);border-bottom-color:var(--text);font-weight:600;}
  .panel{display:none;} .panel.active{display:block;}
  form{background:var(--bg-elev);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:26px;}
  form .row{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap;}
  form .row > div{flex:1;min-width:110px;display:flex;flex-direction:column;gap:6px;}
  label{font-size:11px;color:var(--text-dim);}
  input, select{font-family:var(--font);font-size:14px;padding:10px 11px;border:1px solid var(--border);border-radius:8px;background:var(--bg-elev-2);color:var(--text);}
  .type-toggle{display:flex;gap:8px;}
  .type-toggle button{flex:1;padding:10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-elev-2);cursor:pointer;color:var(--text-dim);}
  .type-toggle button.active-in{background:var(--green-bg);border-color:var(--green);color:var(--green);font-weight:600;}
  .type-toggle button.active-out{background:var(--red-bg);border-color:var(--red);color:var(--red);font-weight:600;}
  .submit-btn{width:100%;padding:12px;background:var(--text);color:var(--bg);border:none;border-radius:8px;font-weight:600;cursor:pointer;}
  .ledger-title{font-size:13px;font-weight:600;color:var(--text-dim);margin:0 0 12px 2px;text-transform:uppercase;}
  .entry{display:flex;align-items:center;gap:12px;padding:12px 4px;border-bottom:1px solid var(--border);}
  .entry .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
  .dot.in{background:var(--green);} .dot.out{background:var(--red);}
  .entry .info{flex:1;min-width:0;} .entry .desc{font-size:14px;}
  .entry .meta{font-size:11.5px;color:var(--text-faint);margin-top:2px;}
  .entry .amt{font-size:14.5px;font-weight:600;white-space:nowrap;}
  .amt.in{color:var(--green);} .amt.out{color:var(--red);}
  .entry .del{background:none;border:none;color:var(--text-faint);cursor:pointer;font-size:15px;}
  .empty{text-align:center;color:var(--text-faint);font-size:13px;padding:32px 10px;}
  .glass{background:var(--bg-elev);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:20px;}
  .glass h3{font-size:13.5px;font-weight:600;margin:0 0 12px 0;}
  .month-select{width:100%;margin-bottom:16px;}
  .cat-row{margin-bottom:14px;}
  .cat-row .cat-head{display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:5px;}
  .bar-bg{height:6px;background:var(--bg-elev-2);border-radius:3px;overflow:hidden;}
  .bar-fill{height:100%;background:var(--accent);border-radius:3px;}
  .tg-toggle-row{display:flex;align-items:center;justify-content:space-between;background:var(--bg-elev-2);border:1px solid var(--border);border-radius:8px;padding:11px 13px;}
  .switch{position:relative;width:38px;height:20px;}
  .switch input{opacity:0;width:0;height:0;}
  .slider{position:absolute;cursor:pointer;inset:0;background:var(--border);border-radius:20px;}
  .slider:before{content:"";position:absolute;height:14px;width:14px;left:3px;top:3px;background:var(--text-dim);border-radius:50%;}
  input:checked + .slider{background:var(--green-bg);border:1px solid var(--green);}
  input:checked + .slider:before{transform:translateX(18px);background:var(--green);}
  .send-report-btn{width:100%;padding:11px;background:transparent;border:1px solid var(--border);color:var(--text);border-radius:8px;cursor:pointer;margin-top:8px;}
  .banner{font-size:12px;padding:10px 12px;border-radius:8px;margin-bottom:18px;background:var(--red-bg);color:var(--red);}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1>Pul Daftari</h1><div class="subtitle" id="userLabel">Kirim-chiqim hisobi</div></div>
  </header>
  <div class="sync-status"><span class="sdot"></span><span>Telegram akkauntingizga bog'langan</span></div>
  <div id="errBanner" class="banner" style="display:none;"></div>

  <div class="balance-card" id="balanceCard">
    <div class="label">Joriy qoldiq</div>
    <div class="amount" id="balanceAmount">0 so'm</div>
  </div>
  <div class="mini-stats">
    <div class="card in"><div class="k">Jami kirim</div><div class="v" id="totalIn">0</div></div>
    <div class="card out"><div class="k">Jami chiqim</div><div class="v" id="totalOut">0</div></div>
  </div>

  <div class="tabs">
    <div class="tab active" data-tab="daftar">Daftar</div>
    <div class="tab" data-tab="tahlil">Tahlil</div>
    <div class="tab" data-tab="sozlama">Sozlama</div>
  </div>

  <div class="panel active" id="panel-daftar">
    <form id="entryForm">
      <div class="row"><div class="type-toggle"><button type="button" id="btnIn" class="active-in">+ Kirim</button><button type="button" id="btnOut">− Chiqim</button></div></div>
      <div class="row">
        <div><label>Summa (so'm)</label><input type="number" id="amountInput" placeholder="masalan: 50000" min="0" step="1" required></div>
        <div><label>Sana</label><input type="date" id="dateInput" required></div>
      </div>
      <div class="row">
        <div><label>Tavsif</label><input type="text" id="descInput" placeholder="masalan: market" required></div>
        <div><label>Kategoriya</label>
          <select id="categoryInput">
            <option>Oziq-ovqat</option><option>Transport</option><option>Uy-joy</option>
            <option>Maosh</option><option>Kommunal</option><option>Kiyim</option>
            <option>Sog'liq</option><option>O'yin-kulgi</option><option>Boshqa</option>
          </select>
        </div>
      </div>
      <button type="submit" class="submit-btn">Daftarga yozish</button>
      <div id="formError" style="display:none;background:var(--red-bg);color:var(--red);font-size:12.5px;padding:9px 11px;border-radius:8px;margin-top:10px;"></div>
    </form>
    <div class="ledger-title">So'nggi yozuvlar</div>
    <div id="entriesList"></div>
  </div>

  <div class="panel" id="panel-tahlil">
    <div class="glass">
      <h3>Oy bo'yicha</h3>
      <select id="monthSelect" class="month-select"></select>
      <div class="mini-stats" style="margin-bottom:16px;">
        <div class="card in"><div class="k">Oylik kirim</div><div class="v" id="monthIn">0</div></div>
        <div class="card out"><div class="k">Oylik chiqim</div><div class="v" id="monthOut">0</div></div>
      </div>
      <h3>Kategoriya bo'yicha chiqim</h3>
      <div id="categoryBreakdown"></div>
    </div>
  </div>

  <div class="panel" id="panel-sozlama">
    <div class="glass">
      <h3>Bildirishnomalar</h3>
      <div class="tg-toggle-row">
        <span style="font-size:13px;">Har bir yozuvda Telegram xabari</span>
        <label class="switch"><input type="checkbox" id="autoNotify"><span class="slider"></span></label>
      </div>
      <button class="send-report-btn" id="sendReportBtn">Umumiy hisobotni hozir yuborish</button>
    </div>
  </div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if(tg){ tg.ready(); tg.expand(); }
const initData = tg?.initData || "";
const tgUser = tg?.initDataUnsafe?.user;
if(tgUser) document.getElementById('userLabel').textContent = "Salom, " + (tgUser.first_name || "");

let state = { transactions: [], autoNotify: false };
let currentType = 'in';

async function api(path, opts={}){
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type':'application/json', 'X-Telegram-Init-Data': initData, ...(opts.headers||{}) }
  });
  if(!res.ok){ throw new Error((await res.json()).detail || 'Xatolik'); }
  return res.json();
}

function fmt(n){ return Math.round(n).toLocaleString('uz-UZ') + " so'm"; }
function escapeHtml(str){ const d=document.createElement('div'); d.textContent=str; return d.innerHTML; }

document.querySelectorAll('.tab').forEach(tab=>{
  tab.addEventListener('click', ()=>{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-'+tab.dataset.tab).classList.add('active');
    if(tab.dataset.tab==='tahlil') renderAnalytics();
  });
});

const btnIn=document.getElementById('btnIn'), btnOut=document.getElementById('btnOut');
btnIn.addEventListener('click', ()=>{ currentType='in'; btnIn.classList.add('active-in'); btnOut.classList.remove('active-out'); });
btnOut.addEventListener('click', ()=>{ currentType='out'; btnOut.classList.add('active-out'); btnIn.classList.remove('active-in'); });
document.getElementById('dateInput').value = new Date().toISOString().slice(0,10);

async function loadState(){
  if(!initData){
    document.getElementById('errBanner').textContent = "Bu sahifa faqat Telegram bot ichida ishlaydi.";
    document.getElementById('errBanner').style.display='block';
    return;
  }
  try{
    state = await api('/api/state');
    document.getElementById('autoNotify').checked = !!state.autoNotify;
    render();
  }catch(e){
    document.getElementById('errBanner').textContent = "Ulanishda xatolik: " + e.message;
    document.getElementById('errBanner').style.display='block';
  }
}

document.getElementById('autoNotify').addEventListener('change', async (e)=>{
  await api('/api/settings', { method:'POST', body: JSON.stringify({autoNotify: e.target.checked}) });
});

document.getElementById('sendReportBtn').addEventListener('click', async ()=>{
  await api('/api/report', { method:'POST' });
  if(tg) tg.showAlert('Hisobot Telegramga yuborildi ✓'); else alert('Yuborildi');
});

function render(){
  const totalIn = state.transactions.filter(t=>t.type==='in').reduce((s,t)=>s+t.amount,0);
  const totalOut = state.transactions.filter(t=>t.type==='out').reduce((s,t)=>s+t.amount,0);
  document.getElementById('totalIn').textContent = fmt(totalIn);
  document.getElementById('totalOut').textContent = fmt(totalOut);
  document.getElementById('balanceAmount').textContent = fmt(totalIn-totalOut);
  document.getElementById('balanceCard').classList.toggle('negative', totalIn-totalOut<0);

  const sorted = [...state.transactions].sort((a,b)=> new Date(b.date)-new Date(a.date) || b.id-a.id);
  const list = document.getElementById('entriesList');
  if(sorted.length===0){ list.innerHTML='<div class="empty">Hali yozuv yo\'q.</div>'; return; }
  list.innerHTML = sorted.map(t=>`
    <div class="entry">
      <div class="dot ${t.type}"></div>
      <div class="info"><div class="desc">${escapeHtml(t.desc)}</div><div class="meta">${t.date} · ${escapeHtml(t.category)}</div></div>
      <div class="amt ${t.type}">${t.type==='in'?'+':'−'} ${fmt(t.amount)}</div>
      <button class="del" data-id="${t.id}">✕</button>
    </div>`).join('');
  list.querySelectorAll('.del').forEach(btn=>{
    btn.addEventListener('click', async ()=>{
      state = await api('/api/transactions/'+btn.dataset.id, {method:'DELETE'});
      render();
    });
  });
}

function monthKey(d){ return d.slice(0,7); }
function renderAnalytics(){
  const sel = document.getElementById('monthSelect');
  const months = [...new Set(state.transactions.map(t=>monthKey(t.date)))].sort().reverse();
  const cur = sel.value;
  sel.innerHTML = months.length ? months.map(m=>`<option value="${m}">${m}</option>`).join('') : '<option value="">Ma\'lumot yo\'q</option>';
  if(months.includes(cur)) sel.value = cur;
  const monthTx = state.transactions.filter(t=>monthKey(t.date)===sel.value);
  document.getElementById('monthIn').textContent = fmt(monthTx.filter(t=>t.type==='in').reduce((s,t)=>s+t.amount,0));
  document.getElementById('monthOut').textContent = fmt(monthTx.filter(t=>t.type==='out').reduce((s,t)=>s+t.amount,0));
  const byCat={};
  monthTx.filter(t=>t.type==='out').forEach(t=>{ byCat[t.category]=(byCat[t.category]||0)+t.amount; });
  const max = Math.max(1, ...Object.values(byCat));
  const entries = Object.entries(byCat).sort((a,b)=>b[1]-a[1]);
  const c = document.getElementById('categoryBreakdown');
  c.innerHTML = entries.length ? entries.map(([cat,val])=>`
    <div class="cat-row"><div class="cat-head"><span>${escapeHtml(cat)}</span><span>${fmt(val)}</span></div>
    <div class="bar-bg"><div class="bar-fill" style="width:${(val/max*100).toFixed(1)}%"></div></div></div>`).join('')
    : '<div class="empty">Bu oyda chiqim yo\'q.</div>';
}
document.getElementById('monthSelect').addEventListener('change', renderAnalytics);

document.getElementById('entryForm').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const errorBox = document.getElementById('formError');
  errorBox.style.display='none';
  const amount = parseFloat(document.getElementById('amountInput').value);
  const date = document.getElementById('dateInput').value;
  const desc = document.getElementById('descInput').value.trim();
  const category = document.getElementById('categoryInput').value;
  const problems=[];
  if(!amount || isNaN(amount) || amount<=0) problems.push("Summani to'g'ri kiriting");
  if(!desc) problems.push("Tavsifni to'ldiring");
  if(!date) problems.push("Sanani tanlang");
  if(problems.length){ errorBox.textContent='⚠ '+problems.join(' · '); errorBox.style.display='block'; return; }

  try{
    state = await api('/api/transactions', { method:'POST', body: JSON.stringify({type:currentType, amount, date, desc, category}) });
    render();
    document.getElementById('amountInput').value='';
    document.getElementById('descInput').value='';
  }catch(e){
    errorBox.textContent = '⚠ ' + e.message;
    errorBox.style.display='block';
  }
});

loadState();
</script>
</body>
</html>"""
