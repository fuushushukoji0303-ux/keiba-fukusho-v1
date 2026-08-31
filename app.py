# -*- coding: utf-8 -*-
"""
地方競馬 単勝＋複勝 1頭勝負 v2.5 300円固定版

- NAR公式サイトの当日単勝・複勝オッズを取得
- 1レース1頭の本命を提示（単勝100円＋複勝200円）
- S / A / B / 見送り判定（S/Aのみ購入候補）
- 候補評価・参考EV・優先度・推奨購入額
- 購入記録、的中/ハズレ、払戻、回収率、競馬場別/ランク別/オッズ帯別集計
- スマホ/PCレスポンシブ
- MEMBER_ID / MEMBER_PASSWORD が設定されていれば会員ログイン保護

注意:
このv1は市場オッズを中心にしたルールベース評価です。
馬の能力・調教・騎手・馬場適性等を網羅した予測モデルではありません。
的中や利益を保証するものではありません。
"""
from __future__ import annotations

import html
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

from flask import Flask, request, redirect, url_for, session

JST = timezone(timedelta(hours=9))
APP_TITLE = "地方競馬 単勝＋複勝 1頭勝負 v2.8 300円固定＋スマホ検証版"
DAILY_LIMIT = 3000
DEFAULT_BET = 300
NAR_BASE_URL = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo"
SPAT4_URL = "https://www.spat4.jp/keiba/pc"

NAR_COURSE_CODES = {
    "門別": 36, "盛岡": 10, "水沢": 11, "浦和": 18, "船橋": 19,
    "大井": 20, "川崎": 21, "金沢": 22, "笠松": 23, "名古屋": 24,
    "園田": 27, "姫路": 28, "高知": 31, "佐賀": 32,
}

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "fukusho_v1.sqlite3")))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-before-selling")
MEMBER_ID = os.environ.get("MEMBER_ID", "").strip()
MEMBER_PASSWORD = os.environ.get("MEMBER_PASSWORD", "").strip()
LOGIN_ENABLED = bool(MEMBER_ID and MEMBER_PASSWORD)


def now(): return datetime.now(JST)
def today(): return now().strftime("%Y-%m-%d")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS draft(
            id INTEGER PRIMARY KEY CHECK(id=1), saved_at TEXT,
            course TEXT, race TEXT, horse_no INTEGER, horse_name TEXT,
            place_low REAL, place_high REAL, grade TEXT, score INTEGER,
            ev_index REAL, amount INTEGER
        );
        CREATE TABLE IF NOT EXISTS purchases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, race_date TEXT NOT NULL,
            course TEXT NOT NULL, race TEXT NOT NULL,
            horse_no INTEGER NOT NULL, horse_name TEXT NOT NULL,
            place_low REAL NOT NULL, place_high REAL NOT NULL,
            grade TEXT NOT NULL, score INTEGER NOT NULL, ev_index REAL NOT NULL,
            amount INTEGER NOT NULL, result TEXT NOT NULL DEFAULT '未確定',
            return_amount INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS picks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_date TEXT NOT NULL, saved_at TEXT NOT NULL,
            course TEXT NOT NULL, race TEXT NOT NULL,
            horse_no INTEGER NOT NULL, horse_name TEXT NOT NULL,
            place_low REAL NOT NULL, place_high REAL NOT NULL,
            grade TEXT NOT NULL, score INTEGER NOT NULL, ev_index REAL NOT NULL,
            UNIQUE(race_date, course, race)
        );
        CREATE TABLE IF NOT EXISTS verifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_id INTEGER NOT NULL UNIQUE,
            verified_at TEXT NOT NULL,
            finish_pos INTEGER NOT NULL,
            win_payout INTEGER NOT NULL DEFAULT 0,
            place_payout INTEGER NOT NULL DEFAULT 0,
            bet_amount INTEGER NOT NULL DEFAULT 0,
            return_amount INTEGER NOT NULL DEFAULT 0,
            profit INTEGER NOT NULL DEFAULT 0
        );
        """)
init_db()


def to_int(v, default=0):
    try: return int(float(str(v).replace(",", "").strip()))
    except Exception: return default

def to_float(v, default=0.0):
    try: return float(str(v).replace(",", "").strip())
    except Exception: return default


def fixed_amount(grade, remaining, low):
    """現行ルールの購入額を一元管理。S/Aのみ300円、B/見送りは0円。"""
    if grade not in ("S", "A"):
        return 0
    if to_float(low, 0.0) < 1.5:
        return 0
    if to_int(remaining, 0) < 300:
        return 0
    return 300


def get_draft():
    with db() as con:
        r = con.execute("SELECT * FROM draft WHERE id=1").fetchone()
    if not r:
        return {}
    d = dict(r)
    # 旧版の700円/1000円等がDBに残っていても必ず現行ルールへ正規化
    d["amount"] = fixed_amount(d.get("grade"), 300, d.get("place_low"))
    return d


def save_draft(item, course, race, grade, score, amount):
    # フォームから渡された金額は信用せず、ここで300円/0円へ強制
    amount = fixed_amount(grade, 300, item.get("place_low"))
    with db() as con:
        con.execute("""
        INSERT INTO draft(id,saved_at,course,race,horse_no,horse_name,place_low,place_high,grade,score,ev_index,amount)
        VALUES(1,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          saved_at=excluded.saved_at,course=excluded.course,race=excluded.race,
          horse_no=excluded.horse_no,horse_name=excluded.horse_name,
          place_low=excluded.place_low,place_high=excluded.place_high,
          grade=excluded.grade,score=excluded.score,ev_index=excluded.ev_index,amount=excluded.amount
        """, (now().strftime("%Y-%m-%d %H:%M:%S"), course, f"{race}R",
              item["horse_no"], item["horse_name"], item["place_low"], item["place_high"],
              grade, score, item["ev_index"], amount))


def summary():
    with db() as con:
        rows = con.execute("SELECT * FROM purchases WHERE race_date=?", (today(),)).fetchall()
    bet = sum(int(r["amount"]) for r in rows)
    ret = sum(int(r["return_amount"]) for r in rows if r["result"] == "的中")
    return {"bet": bet, "return": ret, "profit": ret-bet, "remaining": max(0, DAILY_LIMIT-bet)}


class SimpleTableParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[]; self.row=None; self.cell=None; self.parts=[]
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row=[]
        elif tag in ("td","th") and self.row is not None: self.cell=tag; self.parts=[]
    def handle_data(self, data):
        if self.cell is not None: self.parts.append(data)
    def handle_endtag(self, tag):
        if tag in ("td","th") and self.cell is not None:
            self.row.append(" ".join(" ".join(self.parts).replace("\xa0"," ").split()))
            self.cell=None; self.parts=[]
        elif tag == "tr" and self.row is not None:
            if self.row: self.rows.append(self.row)
            self.row=None


def nar_fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 AppleWebKit/605.1.15 Safari/604.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset() or "utf-8"
    try: return raw.decode(charset, errors="replace")
    except Exception: return raw.decode("utf-8", errors="replace")


def nar_date_text(): return now().strftime("%Y/%m/%d")

def nar_url(page_name, course, race):
    q = urllib.parse.urlencode({"k_babaCode":NAR_COURSE_CODES[course], "k_raceDate":nar_date_text(), "k_raceNo":int(race)})
    return f"{NAR_BASE_URL}/{page_name}?{q}"


def race_numbers(course):
    q = urllib.parse.urlencode({"k_babaCode":NAR_COURSE_CODES[course], "k_raceDate":nar_date_text()})
    text = nar_fetch(f"{NAR_BASE_URL}/RaceList?{q}")
    nums={int(x) for x in re.findall(r"k_raceNo=(\d+)", text) if 1<=int(x)<=12}
    if not nums:
        plain=re.sub(r"<[^>]+>"," ",text)
        nums={int(x) for x in re.findall(r"(?<!\d)(1[0-2]|[1-9])R(?!\d)",plain)}
    return sorted(nums)


def nar_get_horses(course, race):
    text = nar_fetch(nar_url("OddsTanFuku", course, race))
    p=SimpleTableParser(); p.feed(text); horses=[]
    for row in p.rows:
        if len(row)<5: continue
        no=row[1].replace(" ","")
        if not re.fullmatch(r"\d{1,2}", no): continue
        name=row[2].strip()
        if not name or "馬名" in name: continue
        win_nums=re.findall(r"\d+(?:\.\d+)?",row[3]); place_nums=re.findall(r"\d+(?:\.\d+)?",row[4])
        if not win_nums or not place_nums: continue
        win=to_float(win_nums[0],0); low=to_float(place_nums[0],0); high=to_float(place_nums[1] if len(place_nums)>=2 else place_nums[0],0)
        if win<=0 or low<=0: continue
        horses.append({"horse_no":int(no),"horse_name":name,"win_odds":win,"place_low":low,"place_high":max(low,high)})
    horses.sort(key=lambda x:x["win_odds"])
    for i,h in enumerate(horses,1): h["market_rank"]=i
    return horses


def history_calibration():
    with db() as con:
        rows=con.execute("SELECT result,amount,return_amount FROM purchases WHERE result IN ('的中','ハズレ')").fetchall()
    n=len(rows); hits=sum(1 for r in rows if r["result"]=="的中"); bet=sum(int(r["amount"]) for r in rows); ret=sum(int(r["return_amount"]) for r in rows)
    return {"n":n,"hit_rate":hits/n if n else None,"roi":ret/bet if bet else None}


def score_horses(horses):
    """市場オッズ中心の初版スコア。高人気だけを機械的に買わず、複勝妙味と安定性も見る。"""
    cal=history_calibration(); out=[]
    for h in horses:
        low=max(h["place_low"],0.1); high=max(h["place_high"],low); mid=(low+high)/2
        spread=(high-low)/low
        rank=h["market_rank"]

        # 候補評価: 上位人気、複勝レンジ、オッズ幅を組み合わせる。
        rank_score=max(0,100-(rank-1)*10)
        sweet=max(0,100-abs(mid-1.9)*35)  # 初版は1.9倍付近を妙味中心に置く
        stability=max(0,100-spread*100)
        confidence=max(1,min(99,round(rank_score*0.50+sweet*0.30+stability*0.20)))

        # 市場確率(1/mid)と候補評価を控えめに混合した「参考」確率・EV
        market_p=min(0.95,1.0/mid)
        model_p=confidence/100
        model_weight=0.20 + min(0.20, cal["n"]/500)
        est_p=model_p*model_weight + market_p*(1-model_weight)
        if cal["n"]>=50 and cal["roi"] is not None:
            est_p*=max(0.94,min(1.05,0.97+0.03*cal["roi"]))
        est_p=max(0.01,min(0.95,est_p))
        ev=est_p*mid
        ev_score=max(0,min(100,(ev-0.85)/0.45*100))
        priority=round(confidence*0.60+ev_score*0.30+stability*0.10,1)
        x=dict(h); x.update({"mid":mid,"spread":spread,"confidence":confidence,"estimated_hit_pct":round(est_p*100,1),"ev_index":round(ev,2),"priority_score":priority,
                            "ev_label":"妙味あり" if ev>=1.08 else "中立" if ev>=0.95 else "妙味薄め"})
        out.append(x)
    out.sort(key=lambda x:(x["priority_score"],x["confidence"],x["ev_index"]),reverse=True)
    return out


def evaluate(horses, remaining):
    ranked=score_horses(horses)
    if not ranked:
        return {"grade":"見送り","score":0,"recs":[],"reasons":["候補を取得できませんでした。"]}
    best=ranked[0]
    score=int(round(best["priority_score"]))

    # 1頭勝負：S/Aのみ購入対象。Bは観察用。
    # 複勝200円が下限1.5倍なら、2～3着時でも300円回収の目安になる。
    if remaining < 300:
        grade="見送り"
    elif score>=88 and best["confidence"]>=84 and best["market_rank"]<=3 and best["place_low"]>=1.5 and best["spread"]<=0.35:
        grade="S"
    elif score>=80 and best["confidence"]>=76 and best["market_rank"]<=4 and best["place_low"]>=1.5:
        grade="A"
    elif score>=72 and best["confidence"]>=68 and best["market_rank"]<=5:
        grade="B"
    else:
        grade="見送り"

    place_only_low=int(200*best["place_low"])-300
    first_low=int(100*best["win_odds"]+200*best["place_low"])-300
    reasons=[
        f"候補評価：{best['confidence']}点",
        f"優先度：{best['priority_score']:.1f}",
        f"単勝オッズ：{best['win_odds']:.1f}倍",
        f"複勝オッズ：{best['place_low']:.1f}～{best['place_high']:.1f}倍",
        f"参考EV：{best['ev_index']:.2f}",
        f"単勝人気順位：{best['market_rank']}位",
        f"2～3着時の下限損益目安：{place_only_low:+,}円",
        f"1着時の下限損益目安：{first_low:+,}円",
    ]
    # 画面へ返す候補も本命1頭だけに限定
    return {"grade":grade,"score":score,"recs":[best],"reasons":reasons}


def recommended_amount(grade, remaining, low):
    return fixed_amount(grade, remaining, low)


def save_pick(course,race,result):
    if not result["recs"]: return
    b=result["recs"][0]
    with db() as con:
        con.execute("""INSERT INTO picks(race_date,saved_at,course,race,horse_no,horse_name,place_low,place_high,grade,score,ev_index)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(race_date,course,race) DO UPDATE SET
        saved_at=excluded.saved_at,horse_no=excluded.horse_no,horse_name=excluded.horse_name,place_low=excluded.place_low,place_high=excluded.place_high,grade=excluded.grade,score=excluded.score,ev_index=excluded.ev_index""",
        (today(),now().strftime("%Y-%m-%d %H:%M:%S"),course,f"{race}R",b["horse_no"],b["horse_name"],b["place_low"],b["place_high"],result["grade"],result["score"],b["ev_index"]))


CSS="""
:root{--bg:#f3f6fa;--card:#fff;--ink:#17202d;--muted:#68778c;--line:#dce4ee;--blue:#1677ff;--green:#16834f;--red:#b42318;--gold:#a56500}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Yu Gothic",sans-serif}.wrap{max-width:980px;margin:auto;padding:12px}.head{display:flex;justify-content:space-between;gap:8px;align-items:center;margin:4px 0 12px}h1{font-size:22px;margin:0}.badge{background:#e8f7ee;color:#17723c;border-radius:99px;padding:6px 9px;font-weight:800;font-size:12px}.nav{display:flex;gap:7px;overflow:auto;margin-bottom:10px}.card{background:#fff;border:1px solid var(--line);border-radius:15px;padding:14px;margin-bottom:10px;box-shadow:0 2px 8px #17202d0b}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.metric small{color:var(--muted);display:block}.metric strong{font-size:21px}.title{font-weight:900;font-size:18px;margin-bottom:10px}.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}label{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}input,select{width:100%;font-size:16px;padding:11px;border:1px solid #cbd6e2;border-radius:10px;background:#fff}button,.btn{border:0;border-radius:10px;background:var(--blue);color:#fff;padding:11px 13px;font-weight:800;text-decoration:none;display:inline-block;font-size:14px}.secondary{background:#edf2f7;color:#26384d}.green{background:var(--green)}.red{background:var(--red)}.gold{background:var(--gold)}.actions{display:flex;gap:7px;flex-wrap:wrap}.note,.ok,.bad{padding:11px;border-radius:11px;margin-bottom:10px;font-size:13px;line-height:1.6}.note{background:#fff7e5;border:1px solid #efd196;color:#704600}.ok{background:#eaf8ef;border:1px solid #a9d9b9;color:#155d31}.bad{background:#fff0ef;border:1px solid #efbbb5;color:#7d2118}.grade{font-size:42px;font-weight:950}.score{font-size:18px;font-weight:800;color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid #e5ebf1;text-align:left}.scroll{overflow:auto}.horse-card{border:1px solid #d7e2ee;border-radius:18px;padding:14px;margin-bottom:12px}.horse-no{font-size:30px;font-weight:950}.horse-name{font-size:20px;font-weight:900}.pick-grid,.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}.pick-grid>div,.stats-grid>div{background:#f5f8fb;border-radius:12px;padding:10px}.pick-grid span,.stats-grid span{display:block;color:var(--muted);font-size:12px}.pick-grid strong,.stats-grid strong{display:block;font-size:18px}.member-status{margin:8px 0 12px;padding:8px 12px;border-radius:10px;background:#eef6ff;color:#375a7f;font-size:13px}.member-status.setup{background:#fff8e8;color:#775112}.small{font-size:12px;color:var(--muted)}.verify-form{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;align-items:end}.verify-form button{min-height:44px}.verify-summary{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.verify-summary>div{background:#f5f8fb;border-radius:12px;padding:10px}.verify-summary span{display:block;color:var(--muted);font-size:12px}.verify-summary strong{display:block;font-size:18px}
@media(max-width:760px){.wrap{padding:10px}.verify-form{grid-template-columns:1fr 1fr}.verify-summary{grid-template-columns:1fr 1fr}.verify-form input,.verify-form select{min-height:48px}.verify-form button{grid-column:1/-1;min-height:52px;font-size:16px}.head h1{font-size:27px}.nav{display:grid;grid-template-columns:1fr 1fr}.nav .btn{text-align:center;min-height:52px;display:flex;align-items:center;justify-content:center}.grid,.pick-grid,.stats-grid{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}.metric strong{font-size:18px}.desktop{display:none}.horse-no{font-size:28px}}
"""


def page(body,title=APP_TITLE):
    member=(f'<div class="member-status">会員ログイン中：{html.escape(str(session.get("member_id","")))}　<a href="/logout">ログアウト</a></div>' if LOGIN_ENABLED and session.get("member_authenticated") else ('<div class="member-status setup">販売前：会員ログイン未設定</div>' if not LOGIN_ENABLED else ''))
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="地方競馬複勝"><title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class="wrap"><div class="head"><h1>{APP_TITLE}</h1><span class="badge">v2.8・スマホ検証</span></div><div class="nav"><a class="btn secondary" href="/">ホーム</a><a class="btn secondary" href="/analyze">複勝1頭予想</a><a class="btn secondary" href="/picks">今日の候補</a><a class="btn secondary" href="/history">成績履歴</a><a class="btn secondary" href="/analytics">成績分析</a><a class="btn secondary" href="/verify">スマホ検証</a><a class="btn secondary" href="/courses">本日の開催</a></div>{member}{body}<div class="note">このv2.8は市場オッズ中心のルールベース参考評価です。的中・利益を保証しません。実際の投票・最終確認は公式投票サイトでご自身で行ってください。</div></div></body></html>'''


def login_page(message=""):
    msg=f'<div class="bad">{html.escape(message)}</div>' if message else ''
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>会員ログイン</title><style>{CSS}</style></head><body><div class="wrap" style="max-width:520px;padding-top:60px"><div class="card"><div class="title">会員ログイン</div>{msg}<form method="post"><label>会員ID</label><input name="member_id" required><br><br><label>パスワード</label><input type="password" name="password" required><br><br><button class="green" style="width:100%">ログイン</button></form></div></div></body></html>'''


@app.before_request
def require_login():
    if request.endpoint in ("login","logout","health","static"): return None
    if LOGIN_ENABLED and session.get("member_authenticated") is not True: return redirect(url_for("login",next=request.path))

@app.route("/login",methods=["GET","POST"])
def login():
    if not LOGIN_ENABLED: return redirect(url_for("home"))
    if request.method=="POST":
        if request.form.get("member_id","").strip()==MEMBER_ID and request.form.get("password","")==MEMBER_PASSWORD:
            session["member_authenticated"]=True; session["member_id"]=MEMBER_ID
            return redirect(request.args.get("next") or url_for("home"))
        return login_page("会員IDまたはパスワードが違います。")
    return login_page()

@app.get("/logout")
def logout(): session.clear(); return redirect(url_for("login"))

@app.get("/")
def home():
    s=summary(); d=get_draft(); msg=request.args.get("msg",""); msg_html=f'<div class="ok">{html.escape(msg)}</div>' if msg else ''
    draft=''
    if d:
        draft=f'''<div class="card"><div class="title">現在の本命1頭</div><div class="horse-card"><div class="horse-no">{d.get('horse_no','')}番</div><div class="horse-name">{html.escape(str(d.get('horse_name','')))}</div><div class="pick-grid"><div><span>複勝オッズ</span><strong>{float(d.get('place_low') or 0):.1f}～{float(d.get('place_high') or 0):.1f}倍</strong></div><div><span>判定</span><strong>{html.escape(str(d.get('grade','')))}</strong></div><div><span>参考EV</span><strong>{float(d.get('ev_index') or 0):.2f}</strong></div><div><span>推奨購入額</span><strong>{int(d.get('amount') or 0):,}円</strong></div><div><span>買い方</span><strong>単勝100円＋複勝200円</strong></div></div></div><form method="post" action="/record"><button class="green">この1頭を購入記録へ</button></form></div>'''
    reset_card = '''<div class="card" style="border:2px solid #efbbb5"><div class="title">今日のデータをリセット</div><div class="small">今日の購入履歴・今日の候補・ホームの本命だけを削除します。過去日の成績は残ります。</div><br><form method="post" action="/reset-today" onsubmit="return confirm('今日のデータをリセットします。過去日の成績は残ります。よろしいですか？');"><button class="red" style="width:100%;font-size:16px">今日の使用額を0円にリセット</button></form><div class="small" style="margin-top:8px">リセット後：使用額0円／残り予算3,000円／本日の収支0円</div></div>'''
    return page(f'''{msg_html}<div class="grid"><div class="card metric"><small>本日の上限</small><strong>{DAILY_LIMIT:,}円</strong></div><div class="card metric"><small>使用額</small><strong>{s['bet']:,}円</strong></div><div class="card metric"><small>残り予算</small><strong>{s['remaining']:,}円</strong></div><div class="card metric"><small>本日の収支</small><strong>{s['profit']:+,}円</strong></div></div><div class="card"><div class="title">複勝1頭予想</div><div class="actions"><a class="btn green" href="/analyze">オッズ取得 → 1頭予想</a><a class="btn gold" href="/picks">今日の候補を見る</a></div></div>{reset_card}{draft}''')

@app.route("/analyze",methods=["GET","POST"])
def analyze():
    course=request.values.get("course",""); race=to_int(request.values.get("race",""),0)
    opts=''.join(f'<option {"selected" if c==course else ""}>{c}</option>' for c in NAR_COURSE_CODES)
    ropts=''.join(f'<option value="{n}" {"selected" if n==race else ""}>{n}R</option>' for n in range(1,13))
    form=f'''<div class="card"><div class="title">単勝＋複勝オッズ取得 → 本命1頭予想</div><form method="post"><div class="two"><div><label>競馬場</label><select name="course"><option value="">選択</option>{opts}</select></div><div><label>レース</label><select name="race"><option value="">選択</option>{ropts}</select></div></div><br><button class="green">本命1頭を分析</button></form></div>'''
    if request.method=="GET" and request.args.get("auto")!="1": return page(form,"単勝＋複勝 1頭予想")
    if course not in NAR_COURSE_CODES or not 1<=race<=12: return page(form+'<div class="bad">競馬場とレースを選んでください。</div>')
    try: horses=nar_get_horses(course,race)
    except Exception as e: return page(form+f'<div class="bad">取得エラー：{html.escape(type(e).__name__)} - {html.escape(str(e))}</div>')
    if not horses: return page(form+'<div class="note">単勝・複勝オッズを取得できませんでした。発売前・締切後・更新中の可能性があります。</div>')
    result=evaluate(horses,summary()["remaining"]); save_pick(course,race,result); recs=result["recs"]
    reasons=''.join(f'<li>{html.escape(x)}</li>' for x in result["reasons"])
    cards=''
    for x in recs:
        cards+=f'''<div class="horse-card"><div><strong>本日の推奨馬</strong></div><div class="horse-no">{x['horse_no']}番</div><div class="horse-name">{html.escape(x['horse_name'])}</div><div class="pick-grid"><div><span>単勝</span><strong>{x['win_odds']:.1f}倍</strong></div><div><span>複勝</span><strong>{x['place_low']:.1f}～{x['place_high']:.1f}倍</strong></div><div><span>候補評価</span><strong>{x['confidence']}点</strong></div><div><span>優先度</span><strong>{x['priority_score']:.1f}</strong></div><div><span>参考EV</span><strong>{x['ev_index']:.2f}</strong><span>{x['ev_label']}</span></div></div></div>'''
    button=''
    if recs:
        b=recs[0]; amount=recommended_amount(result["grade"],summary()["remaining"],b["place_low"])
        button=f'''<form method="post" action="/apply"><input type="hidden" name="course" value="{html.escape(course)}"><input type="hidden" name="race" value="{race}"><input type="hidden" name="horse_no" value="{b['horse_no']}"><input type="hidden" name="horse_name" value="{html.escape(b['horse_name'])}"><input type="hidden" name="place_low" value="{b['place_low']}"><input type="hidden" name="place_high" value="{b['place_high']}"><input type="hidden" name="grade" value="{result['grade']}"><input type="hidden" name="score" value="{result['score']}"><input type="hidden" name="ev_index" value="{b['ev_index']}"><input type="hidden" name="amount" value="{amount}"><button class="green">単勝100円＋複勝200円をホームへ入力（合計{amount:,}円）</button></form>'''
    return page(form+f'''<div class="card"><div class="title">{html.escape(course)} {race}R 参考判定</div><div class="grade">{result['grade']}</div><div class="score">参考スコア {result['score']} / 100</div><ul>{reasons}</ul><div class="small">※参考EVは実際の的中確率ではありません。初版では市場オッズ中心の参考指数です。</div></div><div class="card"><div class="title">本命1頭</div>{cards}{button}</div>''')

@app.post("/apply")
def apply():
    item={"horse_no":to_int(request.form.get("horse_no")),"horse_name":request.form.get("horse_name",""),"place_low":to_float(request.form.get("place_low")),"place_high":to_float(request.form.get("place_high")),"ev_index":to_float(request.form.get("ev_index"))}
    grade=request.form.get("grade","見送り")
    amount=fixed_amount(grade, summary()["remaining"], item["place_low"])
    save_draft(item,request.form.get("course",""),to_int(request.form.get("race")),grade,to_int(request.form.get("score")),amount)
    return redirect(url_for("home",msg="本命1頭をホームへ入力しました。"))

@app.post("/reset-today")
def reset_today():
    with db() as con:
        con.execute("DELETE FROM purchases WHERE race_date=?", (today(),))
        con.execute("DELETE FROM picks WHERE race_date=?", (today(),))
        con.execute("DELETE FROM draft WHERE id=1")
    return redirect(url_for("home", msg="本日のデータをリセットしました。使用額0円・残り予算3,000円・本日の収支0円です。"))


@app.post("/record")
def record():
    d=get_draft()
    if not d: return redirect(url_for("home",msg="先に本命1頭を分析してください。"))
    amount=fixed_amount(d.get("grade"), summary()["remaining"], d.get("place_low"))
    if amount<100: return redirect(url_for("home",msg="B/見送り判定、または条件不足のため推奨購入額は0円です。"))
    if amount>summary()["remaining"]: return redirect(url_for("home",msg="本日の残り予算を超えています。"))
    with db() as con:
        con.execute("""INSERT INTO purchases(created_at,race_date,course,race,horse_no,horse_name,place_low,place_high,grade,score,ev_index,amount,result,return_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now().strftime("%Y-%m-%d %H:%M:%S"),today(),d["course"],d["race"],d["horse_no"],d["horse_name"],d["place_low"],d["place_high"],d["grade"],d["score"],d["ev_index"],amount,"未確定",0))
    return redirect(url_for("history",msg="購入記録を追加しました。レース後に結果を入力してください。"))

@app.get("/picks")
def picks():
    with db() as con: rows=con.execute("SELECT * FROM picks WHERE race_date=? ORDER BY CASE grade WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 ELSE 9 END,score DESC",(today(),)).fetchall()
    body=''.join(f'''<div class="horse-card"><div class="horse-name">{html.escape(r['course'])} {html.escape(r['race'])}　{r['horse_no']}番 {html.escape(r['horse_name'])}</div><div class="pick-grid"><div><span>判定</span><strong>{r['grade']}</strong></div><div><span>スコア</span><strong>{r['score']}</strong></div><div><span>複勝</span><strong>{r['place_low']:.1f}～{r['place_high']:.1f}</strong></div><div><span>参考EV</span><strong>{r['ev_index']:.2f}</strong></div></div></div>''' for r in rows) or '<div class="note">本日の分析済み候補はまだありません。</div>'
    return page(f'<div class="card"><div class="title">今日の複勝候補</div>{body}</div>')


def stat_box(where="",params=()):
    q="SELECT result,amount,return_amount FROM purchases WHERE result IN ('的中','ハズレ')" + (" AND "+where if where else "")
    with db() as con: rows=con.execute(q,params).fetchall()
    n=len(rows); hits=sum(1 for r in rows if r["result"]=="的中"); bet=sum(int(r["amount"]) for r in rows); ret=sum(int(r["return_amount"]) for r in rows)
    return {"n":n,"hits":hits,"hit_rate":hits/n*100 if n else 0,"bet":bet,"ret":ret,"roi":ret/bet*100 if bet else 0,"profit":ret-bet}

@app.get("/history")
def history():
    msg=request.args.get("msg",""); mh=f'<div class="ok">{html.escape(msg)}</div>' if msg else ''
    with db() as con: rows=con.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT 100").fetchall()
    s=stat_box(); cards=''
    for r in rows:
        if r["result"]=="未確定": action=f'''<form method="post" action="/result/{r['id']}"><label>払戻額（的中時）</label><input name="return_amount" type="number" min="0" step="10" value="0"><br><br><div class="two"><button name="result" value="的中" class="green">的中</button><button name="result" value="ハズレ" class="red">ハズレ</button></div></form>'''
        else: action=f'<div class="ok">{r["result"]}　払戻 {r["return_amount"]:,}円　損益 {int(r["return_amount"])-int(r["amount"]):+,}円</div>'
        cards+=f'''<div class="horse-card"><div class="horse-name">{r['race_date']}　{html.escape(r['course'])} {html.escape(r['race'])}</div><div>{r['horse_no']}番 {html.escape(r['horse_name'])}</div><div class="pick-grid"><div><span>判定</span><strong>{r['grade']}</strong></div><div><span>購入額</span><strong>{r['amount']:,}円</strong></div><div><span>複勝</span><strong>{r['place_low']:.1f}～{r['place_high']:.1f}</strong></div><div><span>参考EV</span><strong>{r['ev_index']:.2f}</strong></div></div><br>{action}</div>'''
    return page(mh+f'''<div class="card"><div class="title">通算成績</div><div class="stats-grid"><div><span>確定</span><strong>{s['n']}R</strong></div><div><span>的中率</span><strong>{s['hit_rate']:.1f}%</strong></div><div><span>回収率</span><strong>{s['roi']:.1f}%</strong></div><div><span>収支</span><strong>{s['profit']:+,}円</strong></div></div></div><div class="card"><div class="title">成績履歴</div>{cards or '<div class="note">購入記録はまだありません。</div>'}</div>''')

@app.post("/result/<int:pid>")
def result(pid):
    res=request.form.get("result",""); ret=max(0,to_int(request.form.get("return_amount"),0)) if res=="的中" else 0
    if res not in ("的中","ハズレ"): return redirect(url_for("history"))
    with db() as con: con.execute("UPDATE purchases SET result=?,return_amount=? WHERE id=?",(res,ret,pid))
    return redirect(url_for("history",msg="結果を更新しました。"))

@app.get("/analytics")
def analytics():
    overall=stat_box()
    with db() as con:
        courses=[r[0] for r in con.execute("SELECT DISTINCT course FROM purchases WHERE result IN ('的中','ハズレ') ORDER BY course")]
        grades=[r[0] for r in con.execute("SELECT DISTINCT grade FROM purchases WHERE result IN ('的中','ハズレ') ORDER BY grade")]
    def cards(items,field):
        out=''
        for x in items:
            s=stat_box(f"{field}=?",(x,)); out+=f'''<div class="horse-card"><div class="horse-name">{html.escape(str(x))}</div><div class="stats-grid"><div><span>レース</span><strong>{s['n']}</strong></div><div><span>的中率</span><strong>{s['hit_rate']:.1f}%</strong></div><div><span>回収率</span><strong>{s['roi']:.1f}%</strong></div><div><span>収支</span><strong>{s['profit']:+,}円</strong></div></div></div>'''
        return out or '<div class="note">確定データがまだありません。</div>'
    # オッズ帯はplace_low基準
    bands=[("1.0～1.4","place_low>=1.0 AND place_low<1.5"),("1.5～1.9","place_low>=1.5 AND place_low<2.0"),("2.0～2.9","place_low>=2.0 AND place_low<3.0"),("3.0以上","place_low>=3.0")]
    bandcards=''
    for label,cond in bands:
        s=stat_box(cond); bandcards+=f'''<div class="horse-card"><div class="horse-name">{label}倍</div><div class="stats-grid"><div><span>レース</span><strong>{s['n']}</strong></div><div><span>的中率</span><strong>{s['hit_rate']:.1f}%</strong></div><div><span>回収率</span><strong>{s['roi']:.1f}%</strong></div><div><span>収支</span><strong>{s['profit']:+,}円</strong></div></div></div>'''
    return page(f'''<div class="card"><div class="title">通算</div><div class="stats-grid"><div><span>確定</span><strong>{overall['n']}</strong></div><div><span>的中率</span><strong>{overall['hit_rate']:.1f}%</strong></div><div><span>回収率</span><strong>{overall['roi']:.1f}%</strong></div><div><span>収支</span><strong>{overall['profit']:+,}円</strong></div></div></div><div class="card"><div class="title">競馬場別</div>{cards(courses,'course')}</div><div class="card"><div class="title">ランク別</div>{cards(grades,'grade')}</div><div class="card"><div class="title">複勝下限オッズ帯別</div>{bandcards}</div>''')

def verification_summary():
    with db() as con:
        rows=con.execute("SELECT bet_amount,return_amount,profit FROM verifications ORDER BY id").fetchall()
    n=len(rows)
    bet=sum(int(r["bet_amount"]) for r in rows)
    ret=sum(int(r["return_amount"]) for r in rows)
    bought=sum(1 for r in rows if int(r["bet_amount"])>0)
    hits=sum(1 for r in rows if int(r["bet_amount"])>0 and int(r["return_amount"])>0)
    return {"n":n,"bought":bought,"hits":hits,"hit_rate":hits/bought*100 if bought else 0,"bet":bet,"ret":ret,"roi":ret/bet*100 if bet else 0,"profit":ret-bet}

@app.route("/verify",methods=["GET","POST"])
def verify():
    msg=""
    if request.method=="POST":
        pick_id=to_int(request.form.get("pick_id"),0)
        finish=max(1,to_int(request.form.get("finish_pos"),0))
        win_payout=max(0,to_int(request.form.get("win_payout"),0))
        place_payout=max(0,to_int(request.form.get("place_payout"),0))
        with db() as con:
            pick=con.execute("SELECT * FROM picks WHERE id=?",(pick_id,)).fetchone()
        if not pick:
            msg="検証対象の予想が見つかりません。"
        else:
            bet_amount=300 if pick["grade"] in ("S","A") else 0
            if bet_amount==0:
                ret=0
            elif finish==1:
                ret=win_payout + place_payout*2
            elif finish in (2,3):
                ret=place_payout*2
            else:
                ret=0
            profit=ret-bet_amount
            with db() as con:
                con.execute("""INSERT INTO verifications(pick_id,verified_at,finish_pos,win_payout,place_payout,bet_amount,return_amount,profit)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(pick_id) DO UPDATE SET verified_at=excluded.verified_at,finish_pos=excluded.finish_pos,
                win_payout=excluded.win_payout,place_payout=excluded.place_payout,bet_amount=excluded.bet_amount,return_amount=excluded.return_amount,profit=excluded.profit""",
                (pick_id,now().strftime("%Y-%m-%d %H:%M:%S"),finish,win_payout,place_payout,bet_amount,ret,profit))
            msg=f"検証を保存しました。仮想損益 {profit:+,}円"
    s=verification_summary()
    with db() as con:
        rows=con.execute("""SELECT p.*,v.finish_pos,v.win_payout,v.place_payout,v.bet_amount,v.return_amount,v.profit
            FROM picks p LEFT JOIN verifications v ON v.pick_id=p.id
            ORDER BY p.race_date DESC,p.saved_at DESC LIMIT 80""").fetchall()
    cards=""
    for r in rows:
        verified=r["finish_pos"] is not None
        result_html=(f'<div class="ok">検証済み：{r["finish_pos"]}着　仮想購入 {int(r["bet_amount"] or 0):,}円　払戻 {int(r["return_amount"] or 0):,}円　損益 {int(r["profit"] or 0):+,}円</div>' if verified else '')
        options=''.join(f'<option value="{n}" {"selected" if verified and int(r["finish_pos"])==n else ""}>{n}着</option>' for n in range(1,13))
        cards+=f'''<div class="horse-card"><div class="horse-name">{html.escape(r['race_date'])} / {html.escape(r['course'])} {html.escape(r['race'])}</div>
        <div class="horse-no">{r['horse_no']}番</div><div class="horse-name">{html.escape(r['horse_name'])}</div>
        <div class="pick-grid"><div><span>判定</span><strong>{r['grade']}</strong></div><div><span>スコア</span><strong>{r['score']}</strong></div><div><span>複勝</span><strong>{r['place_low']:.1f}～{r['place_high']:.1f}</strong></div><div><span>参考EV</span><strong>{r['ev_index']:.2f}</strong></div></div>
        <br>{result_html}<form method="post" class="verify-form"><input type="hidden" name="pick_id" value="{r['id']}">
        <div><label>着順</label><select name="finish_pos">{options}</select></div>
        <div><label>単勝払戻（100円）</label><input inputmode="numeric" type="number" min="0" step="10" name="win_payout" value="{int(r['win_payout'] or 0)}" placeholder="例 440"></div>
        <div><label>複勝払戻（100円）</label><input inputmode="numeric" type="number" min="0" step="10" name="place_payout" value="{int(r['place_payout'] or 0)}" placeholder="例 180"></div>
        <button class="green">このレースを検証保存</button></form></div>'''
    message=f'<div class="ok">{html.escape(msg)}</div>' if msg else ''
    guide='''<div class="note"><strong>スマホ検証の使い方</strong><br>① 予想時に「1頭を分析」すると自動で一覧に残ります。<br>② レース後、着順と公式の100円払戻を入力します。<br>③ S/Aは単勝100円＋複勝200円＝300円で仮想計算。B/見送りは購入0円として観察成績だけ残します。</div>'''
    body=message+f'''<div class="card"><div class="title">スマホ検証ダッシュボード</div><div class="verify-summary">
    <div><span>検証済み</span><strong>{s['n']}R</strong></div><div><span>購入対象</span><strong>{s['bought']}R</strong></div><div><span>的中率</span><strong>{s['hit_rate']:.1f}%</strong></div><div><span>回収率</span><strong>{s['roi']:.1f}%</strong></div><div><span>仮想収支</span><strong>{s['profit']:+,}円</strong></div></div></div>{guide}<div class="card"><div class="title">予想をスマホで検証</div>{cards or '<div class="note">まだ分析済みの予想がありません。先に1頭予想を実行してください。</div>'}</div>'''
    return page(body,"スマホ検証")

@app.get("/courses")
def courses():
    blocks=''
    for c in NAR_COURSE_CODES:
        try: nums=race_numbers(c)
        except Exception: nums=[]
        if nums:
            links=' '.join(f'<a class="btn secondary" href="/analyze?course={urllib.parse.quote(c)}&race={n}&auto=1">{n}R</a>' for n in nums)
            blocks+=f'<div class="card"><div class="title">{html.escape(c)}</div><div class="actions">{links}</div></div>'
    if not blocks: blocks='<div class="note">現在取得できる開催情報がありません。NAR側の公開状況をご確認ください。</div>'
    return page(f'<div class="card"><div class="title">本日の開催</div><div class="small">レース番号を押すと、そのまま複勝1頭分析を実行します。</div></div>{blocks}')

@app.get("/health")
def health(): return "ok v2.5-300yen-fixed",200

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),debug=True)
