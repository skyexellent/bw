import os, sqlite3, secrets, string, hashlib, hmac, json, math, random
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=".", static_url_path="")

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")
DB = os.path.join(os.path.dirname(__file__), "casino.db")
START_BALANCE = 10000
DAILY_REWARD = 1000

# Добавляй сюда каналы для заданий.
# Пример: {"id": "@mychannel", "title": "Мой канал", "reward": 500}
TASK_CHANNELS = [
    {"id": "@your_channel", "title": "Подписка на канал", "reward": 500}
]

ROOM_ALPHABET = string.ascii_uppercase + string.digits

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance INTEGER NOT NULL DEFAULT 10000,
        created_at TEXT NOT NULL,
        last_daily TEXT,
        referred_by INTEGER,
        ref_earned INTEGER NOT NULL DEFAULT 0,
        games INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0,
        wagered INTEGER NOT NULL DEFAULT 0,
        won INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS tasks(
        user_id INTEGER NOT NULL,
        task_id TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        PRIMARY KEY(user_id, task_id)
    );
    CREATE TABLE IF NOT EXISTS rooms(
        code TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS room_players(
        code TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL,
        PRIMARY KEY(code, user_id)
    );
    """)
    con.commit()
    con.close()

def now():
    return datetime.now(timezone.utc)

def iso(dt=None):
    return (dt or now()).isoformat()

def user_row(uid):
    con = db()
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return row

def update_balance(uid, delta):
    con = db()
    row = con.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row["balance"] + delta < 0:
        con.close()
        return False
    con.execute("UPDATE users SET balance=balance+? WHERE id=?", (delta, uid))
    con.commit(); con.close()
    return True

def add_stats(uid, games=0, wins=0, wagered=0, won=0):
    con = db()
    con.execute("""UPDATE users SET games=games+?, wins=wins+?, wagered=wagered+?, won=won+?
                   WHERE id=?""", (games,wins,wagered,won,uid))
    con.commit(); con.close()

def referral_cut(uid, amount):
    u = user_row(uid)
    if not u or not u["referred_by"] or amount <= 0:
        return 0
    # 3% от фактически потраченной ставки/покупки, начисляется пригласившему.
    reward = max(1, int(amount * 0.03))
    con = db()
    con.execute("UPDATE users SET balance=balance+?, ref_earned=ref_earned+? WHERE id=?",
                (reward, reward, u["referred_by"]))
    con.commit(); con.close()
    return reward

def validate_init_data(init_data):
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
        return None, "Укажи BOT_TOKEN в переменной окружения."
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
        received = pairs.pop("hash", None)
        if not received:
            return None, "Нет Telegram hash."
        data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None, "Неверная подпись Telegram."
        auth_date = int(pairs.get("auth_date", "0"))
        if abs(int(now().timestamp()) - auth_date) > 86400:
            return None, "Telegram initData устарел."
        user = json.loads(pairs["user"])
        return user, None
    except Exception as e:
        return None, f"Ошибка Telegram auth: {e}"

def ensure_user(tg):
    uid = int(tg["id"])
    con = db()
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        ref = request.args.get("ref") or request.headers.get("X-Referral")
        refid = int(ref) if ref and str(ref).isdigit() and int(ref) != uid else None
        con.execute("""INSERT INTO users(id,username,first_name,created_at,referred_by)
                       VALUES(?,?,?,?,?)""",
                    (uid,tg.get("username",""),tg.get("first_name",""),iso(),refid))
        con.commit()
    else:
        con.execute("UPDATE users SET username=?, first_name=? WHERE id=?",
                    (tg.get("username",""),tg.get("first_name",""),uid))
        con.commit()
    con.close()
    return uid

def auth():
    init = request.headers.get("X-Telegram-Init-Data", "")
    tg, err = validate_init_data(init)
    if err: return None, err
    return ensure_user(tg), None

def profile(uid):
    u = user_row(uid)
    ref = f"https://t.me/{os.getenv('BOT_USERNAME','YOUR_BOT')}?startapp=ref_{uid}"
    daily = daily_state(u)
    return {
        "id": u["id"], "username": u["username"], "first_name": u["first_name"],
        "balance": u["balance"], "created_at": u["created_at"],
        "games": u["games"], "wins": u["wins"], "wagered": u["wagered"], "won": u["won"],
        "referral": ref, "ref_earned": u["ref_earned"], "daily": daily
    }

def daily_state(u):
    local = datetime.now().astimezone()
    today = local.date().isoformat()
    claimed = bool(u["last_daily"] and u["last_daily"][:10] == today)
    tomorrow = (local.date() + timedelta(days=1))
    reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=local.tzinfo)
    return {"claimed": claimed, "reward": DAILY_REWARD,
            "reset_at": reset.isoformat()}

def require_bet(uid, bet):
    try: bet = int(bet)
    except: return False, "Некорректная ставка."
    if bet < 1 or bet > 1_000_000:
        return False, "Ставка должна быть от 1 до 1 000 000."
    if not update_balance(uid, -bet):
        return False, "Недостаточно монет."
    return True, bet

def finish(uid, bet, payout):
    payout = int(max(0, payout))
    if payout: update_balance(uid, payout)
    add_stats(uid, games=1, wins=1 if payout > 0 else 0, wagered=bet, won=payout)
    referral_cut(uid, bet)
    return payout

@app.get("/")
def index():
    return send_from_directory(".", "index.html")

@app.post("/api/auth")
def api_auth():
    uid, err = auth()
    if err: return jsonify(ok=False,error=err),401
    return jsonify(ok=True, profile=profile(uid), tasks=TASK_CHANNELS)

@app.get("/api/profile")
def api_profile():
    uid, err = auth()
    if err: return jsonify(ok=False,error=err),401
    return jsonify(ok=True, profile=profile(uid))

@app.post("/api/daily")
def daily():
    uid, err = auth()
    if err: return jsonify(ok=False,error=err),401
    u = user_row(uid)
    local = datetime.now().astimezone()
    today = local.date().isoformat()
    if u["last_daily"] and u["last_daily"][:10] == today:
        return jsonify(ok=False,error="Сегодня награда уже получена.")
    con = db()
    con.execute("UPDATE users SET balance=balance+?, last_daily=? WHERE id=?",
                (DAILY_REWARD, iso(), uid))
    con.commit(); con.close()
    return jsonify(ok=True, balance=user_row(uid)["balance"])

@app.post("/api/task/check")
def task_check():
    uid, err = auth()
    if err: return jsonify(ok=False,error=err),401
    data = request.json or {}
    task_id = str(data.get("task_id",""))
    task = next((x for x in TASK_CHANNELS if x["id"] == task_id), None)
    if not task: return jsonify(ok=False,error="Задание не найдено."),404
    con = db()
    done = con.execute("SELECT 1 FROM tasks WHERE user_id=? AND task_id=?", (uid,task_id)).fetchone()
    if done:
        con.close(); return jsonify(ok=False,error="Задание уже выполнено.")
    # Для реальной проверки бот должен быть администратором канала.
    if BOT_TOKEN.startswith("PASTE_"):
        con.close(); return jsonify(ok=False,error="Сначала настрой BOT_TOKEN.")
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
                         params={"chat_id":task["id"],"user_id":uid}, timeout=8).json()
        member = r.get("result", {})
        status = member.get("status")
        if status not in ("creator","administrator","member"):
            con.close(); return jsonify(ok=False,error="Сначала подпишись на канал.")
    except Exception:
        con.close(); return jsonify(ok=False,error="Не удалось проверить подписку.")
    con.execute("INSERT INTO tasks VALUES(?,?,?)", (uid,task_id,iso()))
    con.commit(); con.close()
    update_balance(uid, int(task["reward"]))
    return jsonify(ok=True, balance=user_row(uid)["balance"])

@app.post("/api/game/plinko")
def plinko():
    uid, err = auth()
    if err: return jsonify(ok=False,error=err),401
    d=request.json or {}; ok,bet=require_bet(uid,d.get("bet",0))
    if not ok:return jsonify(ok=False,error=bet)
    rows=max(8,min(16,int(d.get("rows",12))))
    # Симметричные множители; сервер сам определяет конечную ячейку.
    multipliers=[0.25,0.5,0.75,1,1.5,2,4,8,12,8,4,2,1.5,1,0.75,0.5,0.25]
    center=len(multipliers)//2
    pos=0
    path=[]
    for _ in range(rows):
        step=random.choice((-1,1))
        pos=max(-center,min(center,pos+step)); path.append(step)
    idx=max(0,min(len(multipliers)-1,center+pos))
    mult=multipliers[idx]
    payout=int(bet*mult)
    finish(uid,bet,payout)
    return jsonify(ok=True,multiplier=mult,payout=payout,path=path,balance=user_row(uid)["balance"])

@app.post("/api/game/mines/start")
def mines_start():
    uid, err=auth()
    if err:return jsonify(ok=False,error=err),401
    d=request.json or {}; ok,bet=require_bet(uid,d.get("bet",0))
    if not ok:return jsonify(ok=False,error=bet)
    mines=max(1,min(24,int(d.get("mines",5))))
    cells=list(range(25)); random.shuffle(cells)
    mine=sorted(cells[:mines])
    con=db()
    con.execute("CREATE TABLE IF NOT EXISTS mine_rounds(user_id INTEGER PRIMARY KEY, bet INTEGER, mines TEXT, opened TEXT)")
    con.execute("INSERT OR REPLACE INTO mine_rounds VALUES(?,?,?,?)",(uid,bet,json.dumps(mine),json.dumps([])))
    con.commit();con.close()
    return jsonify(ok=True,balance=user_row(uid)["balance"],safe_count=25-mines)

@app.post("/api/game/mines/open")
def mines_open():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    d=request.json or {}; cell=int(d.get("cell",-1))
    con=db()
    con.execute("CREATE TABLE IF NOT EXISTS mine_rounds(user_id INTEGER PRIMARY KEY, bet INTEGER, mines TEXT, opened TEXT)")
    r=con.execute("SELECT * FROM mine_rounds WHERE user_id=?",(uid,)).fetchone()
    if not r: con.close(); return jsonify(ok=False,error="Нет активного раунда.")
    opened=json.loads(r["opened"]); mines=json.loads(r["mines"])
    if cell<0 or cell>=25 or cell in opened: con.close(); return jsonify(ok=False,error="Неверная клетка.")
    if cell in mines:
        con.execute("DELETE FROM mine_rounds WHERE user_id=?",(uid,)); con.commit();con.close()
        add_stats(uid,games=1,wagered=r["bet"]); referral_cut(uid,r["bet"])
        return jsonify(ok=True,mine=True,payout=0,balance=user_row(uid)["balance"])
    opened.append(cell)
    con.execute("UPDATE mine_rounds SET opened=? WHERE user_id=?",(json.dumps(opened),uid))
    con.commit();con.close()
    m=len(opened); mine_count=len(mines)
    # Fair multiplier based on survival probability with 4% house edge.
    mult=(1-0.04) / max(1e-9, math.comb(25,m)/math.comb(25-mine_count,m))
    mult=max(1.01,round(1/max(1e-9, math.prod((25-mine_count-i)/(25-i) for i in range(m)))*0.96,2))
    return jsonify(ok=True,mine=False,multiplier=mult,opened=opened)

@app.post("/api/game/mines/cashout")
def mines_cashout():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    con=db(); r=con.execute("SELECT * FROM mine_rounds WHERE user_id=?",(uid,)).fetchone()
    if not r: con.close(); return jsonify(ok=False,error="Нет активного раунда.")
    opened=json.loads(r["opened"]); mines=len(json.loads(r["mines"])); m=len(opened)
    if m==0: mult=0
    else: mult=max(1.01,round(0.96/max(1e-9,math.prod((25-mines-i)/(25-i) for i in range(m))),2))
    payout=int(r["bet"]*mult)
    con.execute("DELETE FROM mine_rounds WHERE user_id=?",(uid,));con.commit();con.close()
    finish(uid,r["bet"],payout)
    return jsonify(ok=True,multiplier=mult,payout=payout,balance=user_row(uid)["balance"])

@app.post("/api/game/hilo")
def hilo():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    d=request.json or {}; ok,bet=require_bet(uid,d.get("bet",0))
    if not ok:return jsonify(ok=False,error=bet)
    current=random.randint(2,14)
    return jsonify(ok=True,round_id=secrets.token_hex(8),bet=bet,current=current,balance=user_row(uid)["balance"])

@app.post("/api/game/hilo/guess")
def hilo_guess():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    d=request.json or {}; bet=int(d.get("bet",0)); current=int(d.get("current",7)); guess=d.get("guess")
    nxt=random.randint(2,14)
    win=(guess=="hi" and nxt>current) or (guess=="lo" and nxt<current)
    if nxt==current: win=True
    mult=round(1.0 + min(8, abs(nxt-current))*0.18,2)
    payout=int(bet*mult) if win else 0
    finish(uid,bet,payout)
    return jsonify(ok=True,next=nxt,win=win,multiplier=mult,payout=payout,balance=user_row(uid)["balance"])

@app.post("/api/game/upgrader")
def upgrader():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    d=request.json or {}; ok,bet=require_bet(uid,d.get("bet",0))
    if not ok:return jsonify(ok=False,error=bet)
    target=max(1.05,min(100,float(d.get("target",2))))
    chance=min(0.96,0.96/target)
    win=random.random()<chance
    payout=int(bet*target) if win else 0
    finish(uid,bet,payout)
    return jsonify(ok=True,win=win,chance=round(chance*100,2),target=target,payout=payout,balance=user_row(uid)["balance"])

@app.post("/api/game/sweet")
def sweet():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    d=request.json or {}; ok,bet=require_bet(uid,d.get("bet",0))
    if not ok:return jsonify(ok=False,error=bet)
    ante=bool(d.get("ante"))
    if ante:
        # Extra 25% is part of the virtual stake.
        extra=int(bet*0.25)
        if not update_balance(uid,-extra):
            update_balance(uid,bet); return jsonify(ok=False,error="Недостаточно монет для Ante Bet.")
        bet+=extra
    symbols=["🍇","🍉","🍬","🍓","🍋","🍊","🍒","🥝"]
    grid=[random.choice(symbols) for _ in range(6*5)]
    counts={s:grid.count(s) for s in symbols}
    groups=[(s,c) for s,c in counts.items() if c>=8]
    base=sum(min(12,c)*0.15 for _,c in groups)
    bonus=random.random()<0.035
    mult=round(min(25000,max(0.0,base + (random.choice([5,10,25,50,100,250,500,1000]) if bonus else 0))),2)
    # Generic cascade-style slot inspired by fruit slots; not an exact reproduction of any provider.
    payout=int(bet*mult)
    finish(uid,bet,payout)
    return jsonify(ok=True,grid=grid,multiplier=mult,bonus=bonus,payout=payout,balance=user_row(uid)["balance"])

def room_code():
    con=db()
    for _ in range(100):
        c="".join(random.choice(ROOM_ALPHABET) for _ in range(6))
        if not con.execute("SELECT 1 FROM rooms WHERE code=?",(c,)).fetchone():
            con.close(); return c
    con.close(); raise RuntimeError("Не удалось создать комнату")

@app.post("/api/room/create")
def room_create():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    c=room_code(); con=db()
    con.execute("INSERT INTO rooms VALUES(?,?,?)",(c,uid,iso()))
    con.execute("INSERT INTO room_players VALUES(?,?,?)",(c,uid,iso()))
    con.commit();con.close()
    return jsonify(ok=True,code=c)

@app.post("/api/room/join")
def room_join():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    c=str((request.json or {}).get("code","")).upper().strip()
    if len(c)!=6:return jsonify(ok=False,error="Код должен содержать 6 символов.")
    con=db(); room=con.execute("SELECT 1 FROM rooms WHERE code=?",(c,)).fetchone()
    if not room:con.close();return jsonify(ok=False,error="Комната не найдена.")
    con.execute("INSERT OR IGNORE INTO room_players VALUES(?,?,?)",(c,uid,iso()))
    con.commit();con.close()
    return jsonify(ok=True,code=c)

@app.get("/api/room/<code>")
def room_info(code):
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    con=db()
    rows=con.execute("""SELECT u.id,u.username,u.first_name,u.balance
                        FROM room_players rp JOIN users u ON u.id=rp.user_id
                        WHERE rp.code=? ORDER BY rp.joined_at""",(code.upper(),)).fetchall()
    con.close()
    return jsonify(ok=True,players=[dict(x) for x in rows])

@app.get("/api/top")
def top():
    uid,err=auth()
    if err:return jsonify(ok=False,error=err),401
    con=db()
    rich=con.execute("SELECT username,first_name,balance FROM users ORDER BY balance DESC LIMIT 20").fetchall()
    active=con.execute("SELECT username,first_name,games,wagered FROM users ORDER BY games DESC LIMIT 20").fetchall()
    con.close()
    return jsonify(ok=True,rich=[dict(x) for x in rich],active=[dict(x) for x in active])

init_db()

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=True)
