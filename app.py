# -*- coding: utf-8 -*-
"""
地方競馬 単勝＋複勝投票管理 v3.1 - 近走・競馬場・距離適性版

- NAR公式サイトの当日単勝・複勝オッズを取得
- 1レース1頭の本命1頭を提示
- S / A / B / 見送り判定
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

from concurrent.futures import ThreadPoolExecutor, as_completed

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
APP_TITLE = "パカおとパカ美のワクワク競馬 単勝＋複勝"
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
        CREATE TABLE IF NOT EXISTS validation_predictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_date TEXT NOT NULL, recorded_at TEXT NOT NULL,
            course TEXT NOT NULL, race TEXT NOT NULL,
            horse_no INTEGER NOT NULL, horse_name TEXT NOT NULL,
            win_odds REAL NOT NULL DEFAULT 0,
            place_low REAL NOT NULL DEFAULT 0,
            place_high REAL NOT NULL DEFAULT 0,
            grade TEXT NOT NULL, score INTEGER NOT NULL,
            ev_index REAL NOT NULL DEFAULT 0,
            amount INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '未確定',
            return_amount INTEGER NOT NULL DEFAULT 0,
            official_result TEXT DEFAULT '',
            checked_at TEXT DEFAULT '',
            result_source TEXT DEFAULT '',
            UNIQUE(race_date, course, race)
        );
        """)
init_db()


def to_int(v, default=0):
    try: return int(float(str(v).replace(",", "").strip()))
    except Exception: return default

def to_float(v, default=0.0):
    try: return float(str(v).replace(",", "").strip())
    except Exception: return default


def get_draft():
    with db() as con:
        r = con.execute("SELECT * FROM draft WHERE id=1").fetchone()
    return dict(r) if r else {}


def save_draft(item, course, race, grade, score, amount):
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



def nar_get_race_start_time(course_name, race_no):
    try:
        text = nar_fetch(nar_url("DebaTable", course_name, race_no), timeout=12)
    except Exception:
        return None
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)
    m = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*発走", plain)
    if not m:
        return None
    n = now()
    return n.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)


def closing_soon_candidates(window_minutes=5):
    current = now()
    tasks = []
    for c in NAR_COURSE_CODES:
        try:
            nums = race_numbers(c)
        except Exception:
            nums = []
        for r in nums:
            tasks.append((c, r))
    schedule = []
    if not tasks:
        return [], []
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        future_map = {pool.submit(nar_get_race_start_time, c, r): (c, r) for c, r in tasks}
        for f in as_completed(future_map):
            c, r = future_map[f]
            try:
                dt = f.result()
            except Exception:
                dt = None
            if dt:
                schedule.append({"course": c, "race": r, "start_dt": dt,
                                 "minutes": (dt-current).total_seconds()/60})
    schedule.sort(key=lambda x: x["start_dt"])
    return ([x for x in schedule if 0 <= x["minutes"] <= window_minutes],
            [x for x in schedule if x["minutes"] > window_minutes])


def nar_refund_url(course_name, race_no, race_date):
    q = urllib.parse.urlencode({
        "k_babaCode": NAR_COURSE_CODES[course_name],
        "k_raceDate": str(race_date).replace("-", "/"),
        "k_raceNo": int(race_no),
    })
    return "https://sp.keiba.go.jp/KeibaWebSP/TodayRaceInfo/S_RefundMoneyList?" + q


def parse_tanfuku_refunds(text):
    if not text:
        return {"win": {}, "place": {}}
    plain = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    plain = re.sub(r"</(?:td|th|tr|div|p|li)>", "\n", plain, flags=re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = html.unescape(plain)
    out = {"win": {}, "place": {}}
    m = re.search(r"単勝\s*(.*?)(?=複勝|枠複|馬複|$)", plain, flags=re.S)
    if m:
        seg = m.group(1)
        nums = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", seg)
        pays = [int(x.replace(",", "")) for x in re.findall(r"([\d,]+)\s*円", seg)]
        if nums and pays:
            out["win"][int(nums[0])] = pays[0]
    m = re.search(r"複勝\s*(.*?)(?=枠複|馬複|枠単|ワイド|$)", plain, flags=re.S)
    if m:
        seg = m.group(1)
        nums = [int(x) for x in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", seg)]
        pays = [int(x.replace(",", "")) for x in re.findall(r"([\d,]+)\s*円", seg)]
        for no, pay in zip(nums[:3], pays[:3]):
            out["place"][no] = pay
    return out


def nar_get_tanfuku_refunds(course_name, race_no, race_date):
    try:
        return parse_tanfuku_refunds(nar_fetch(nar_refund_url(course_name, race_no, race_date), timeout=15))
    except Exception:
        return {"win": {}, "place": {}}


def settle_tanfuku(horse_no, refunds):
    if not refunds or (not refunds.get("win") and not refunds.get("place")):
        return None
    no = int(horse_no)
    ret = 0
    parts = []
    if no in refunds.get("win", {}):
        pay = int(refunds["win"][no])
        ret += pay
        parts.append(f"単勝 {pay}円")
    if no in refunds.get("place", {}):
        pay = int(refunds["place"][no])
        ret += pay * 2
        parts.append(f"複勝 {pay}円×2")
    return {
        "result": "的中" if parts else "ハズレ",
        "return_amount": ret,
        "official_result": " / ".join(parts) if parts else "対象馬券なし",
    }


def save_validation_prediction(course, race, result, remaining):
    if not result.get("recs"):
        return
    b = result["recs"][0]
    amount = recommended_amount(result["grade"], remaining, b["place_low"])
    with db() as con:
        con.execute("""
        INSERT INTO validation_predictions(
            race_date,recorded_at,course,race,horse_no,horse_name,
            win_odds,place_low,place_high,grade,score,ev_index,amount
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(race_date,course,race) DO NOTHING
        """, (
            today(), now().strftime("%Y-%m-%d %H:%M:%S"),
            course, f"{race}R", b["horse_no"], b["horse_name"],
            b["win_odds"], b["place_low"], b["place_high"],
            result["grade"], result["score"], b["ev_index"], amount
        ))


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


def _record_stats(text, label):
    normalized=str(text).replace("\xa0"," ")
    m=re.search(rf"{re.escape(label)}\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)", normalized)
    if not m: return None
    w,s,t3,o=[int(x) for x in m.groups()]
    total=w+s+t3+o
    rate=((w+s+t3)/total*100.0) if total else None
    return {"wins":w,"seconds":s,"thirds":t3,"others":o,"total":total,"top3_rate":round(rate,1) if rate is not None else None}


def nar_get_form_data(course_name, race_no, horses=None):
    url=nar_url("DebaTable",course_name,race_no)
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36","Accept-Language":"ja-JP,ja;q=0.9"})
    with urllib.request.urlopen(req,timeout=15) as res:
        raw=res.read()
    page_text=None
    for enc in ("utf-8","cp932","shift_jis"):
        try:
            page_text=raw.decode(enc); break
        except UnicodeDecodeError: pass
    if page_text is None: page_text=raw.decode("utf-8",errors="replace")
    plain=re.sub(r"(?is)<script.*?</script>"," ",page_text)
    plain=re.sub(r"(?is)<style.*?</style>"," ",plain)
    plain=re.sub(r"(?is)<br\s*/?>"," ",plain)
    plain=re.sub(r"(?is)<[^>]+>"," ",plain)
    plain=html.unescape(plain).replace("\xa0"," ")
    plain=" ".join(plain.split())
    result={}; positions=[]
    for h in list(horses or []):
        name=str(h.get("horse_name") or "").strip()
        if not name: continue
        pos=plain.find(name)
        if pos>=0: positions.append((pos,int(h["horse_no"]),name))
    positions.sort()
    for i,(pos,horse_no,name) in enumerate(positions):
        next_pos=positions[i+1][0] if i+1<len(positions) else min(len(plain),pos+5000)
        block=plain[pos:next_pos]
        recent=[]
        for m in re.finditer(r"(?:^|\s)(\d{1,2})\s+(\d{2}\.\d{2}\.\d{2})\s+(?:良|稍重|重|不良|稍|晴|曇|雨|雪)",block):
            f=int(m.group(1))
            if 1<=f<=30: recent.append(f)
            if len(recent)>=5: break
        if not recent:
            for m in re.finditer(r"(?:^|\s)(\d{1,2})\s*/\s*\d{1,2}\s+\d+人",block):
                f=int(m.group(1))
                if 1<=f<=30: recent.append(f)
                if len(recent)>=5: break
        result[horse_no]={"recent_finishes":recent[:5],"track":_record_stats(block,"場"),"distance":_record_stats(block,"距")}
    return result


def horse_form_rating(form):
    if not form: return 50.0
    parts=[]
    recent=list(form.get("recent_finishes") or [])[:5]
    if recent:
        weights=[1.40,1.25,1.10,1.00,0.90][:len(recent)]
        vals=[max(0.0,100.0-(max(1,int(f))-1)*12.0) for f in recent]
        parts.append((sum(v*w for v,w in zip(vals,weights))/sum(weights),0.50))
    def adjusted(stat):
        if not stat or stat.get("top3_rate") is None: return None
        rate=max(0.0,min(100.0,float(stat["top3_rate"])))
        n=max(0,int(stat.get("total") or 0)); sw=min(1.0,n/10.0)
        return 50.0+(rate-50.0)*sw
    tr=adjusted(form.get("track")); ds=adjusted(form.get("distance"))
    if tr is not None: parts.append((tr,0.25))
    if ds is not None: parts.append((ds,0.25))
    if not parts: return 50.0
    ws=sum(w for _,w in parts)
    return round(sum(v*w for v,w in parts)/ws,1)


def form_display_values(form):
    form=form or {}
    recent=form.get("recent_finishes") or []
    recent_text="・".join(str(x) for x in recent) if recent else "取得なし"
    def rt(stat):
        if not stat or stat.get("top3_rate") is None: return "取得なし"
        return f'{stat["top3_rate"]:.1f}% ({stat.get("wins",0)}-{stat.get("seconds",0)}-{stat.get("thirds",0)}-{stat.get("others",0)})'
    return recent_text,rt(form.get("track")),rt(form.get("distance"))



def history_calibration():
    with db() as con:
        rows=con.execute("SELECT result,amount,return_amount FROM purchases WHERE result IN ('的中','ハズレ')").fetchall()
    n=len(rows); hits=sum(1 for r in rows if r["result"]=="的中"); bet=sum(int(r["amount"]) for r in rows); ret=sum(int(r["return_amount"]) for r in rows)
    return {"n":n,"hit_rate":hits/n if n else None,"roi":ret/bet if bet else None}


def score_horses(horses, form_data=None):
    cal=history_calibration(); out=[]; form_data=form_data or {}
    for h in horses:
        low=max(h["place_low"],0.1); high=max(h["place_high"],low); mid=(low+high)/2
        spread=(high-low)/low; rank=h["market_rank"]
        rank_score=max(0,100-(rank-1)*10)
        sweet=max(0,100-abs(mid-1.9)*35)
        stability=max(0,100-spread*100)
        confidence=max(1,min(99,round(rank_score*0.50+sweet*0.30+stability*0.20)))
        market_p=min(0.95,1.0/mid); model_p=confidence/100
        model_weight=0.20+min(0.20,cal["n"]/500)
        est_p=model_p*model_weight+market_p*(1-model_weight)
        if cal["n"]>=50 and cal["roi"] is not None:
            est_p*=max(0.94,min(1.05,0.97+0.03*cal["roi"]))
        est_p=max(0.01,min(0.95,est_p)); ev=est_p*mid
        ev_score=max(0,min(100,(ev-0.85)/0.45*100))
        base_priority=round(confidence*0.60+ev_score*0.30+stability*0.10,1)
        form=form_data.get(int(h["horse_no"])) or {}
        form_rating=horse_form_rating(form)
        form_adjust=max(-4.0,min(4.0,(form_rating-50.0)*0.08))
        priority=round(max(0.0,min(100.0,base_priority+form_adjust)),1)
        x=dict(h); x.update({"mid":mid,"spread":spread,"confidence":confidence,"estimated_hit_pct":round(est_p*100,1),"ev_index":round(ev,2),"priority_score":priority,"base_priority_score":base_priority,"form_rating":form_rating,"form_adjust":round(form_adjust,1),"form_data":form,"ev_label":"妙味あり" if ev>=1.08 else "中立" if ev>=0.95 else "妙味薄め"})
        out.append(x)
    out.sort(key=lambda x:(x["priority_score"],x["confidence"],x["ev_index"]),reverse=True)
    return out


def evaluate(horses, remaining, form_data=None):
    ranked=score_horses(horses,form_data)
    if not ranked: return {"grade":"見送り","score":0,"recs":[],"reasons":["候補を取得できませんでした。"]}
    best=ranked[0]; score=int(round(best["priority_score"]))
    if remaining<300: grade="見送り"
    elif score>=88 and best["confidence"]>=84 and best["market_rank"]<=3 and best["place_low"]>=1.5 and best["spread"]<=0.35: grade="S"
    elif score>=80 and best["confidence"]>=76 and best["market_rank"]<=4 and best["place_low"]>=1.5: grade="A"
    elif score>=72 and best["confidence"]>=68 and best["market_rank"]<=5: grade="B"
    else: grade="見送り"
    place_only_low=int(200*best["place_low"])-300
    first_low=int(100*best["win_odds"]+200*best["place_low"])-300
    recent_text,track_text,distance_text=form_display_values(best.get("form_data"))
    reasons=[f"候補評価：{best['confidence']}点",f"従来優先度：{best['base_priority_score']:.1f}",f"実績評価：{best['form_rating']:.1f}点（補正 {best['form_adjust']:+.1f}）",f"近5走：{recent_text}",f"競馬場成績 3着内率：{track_text}",f"距離成績 3着内率：{distance_text}",f"補正後優先度：{best['priority_score']:.1f}",f"単勝オッズ：{best['win_odds']:.1f}倍",f"複勝オッズ：{best['place_low']:.1f}～{best['place_high']:.1f}倍",f"参考EV：{best['ev_index']:.2f}",f"単勝人気順位：{best['market_rank']}位",f"2～3着時の下限損益目安：{place_only_low:+,}円",f"1着時の下限損益目安：{first_low:+,}円"]
    return {"grade":grade,"score":score,"recs":[best],"reasons":reasons}


def recommended_amount(grade, remaining, low):
    # S/Aのみ：単勝100円＋複勝200円＝合計300円
    if grade not in ("S","A") or low<1.5 or remaining<300:
        return 0
    return 300


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
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Yu Gothic",sans-serif}.wrap{max-width:980px;margin:auto;padding:12px}.head{display:flex;justify-content:space-between;gap:8px;align-items:center;margin:4px 0 12px}h1{font-size:22px;margin:0}.badge{background:#e8f7ee;color:#17723c;border-radius:99px;padding:6px 9px;font-weight:800;font-size:12px}.nav{display:flex;gap:7px;overflow:auto;margin-bottom:10px}.card{background:#fff;border:1px solid var(--line);border-radius:15px;padding:14px;margin-bottom:10px;box-shadow:0 2px 8px #17202d0b}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.metric small{color:var(--muted);display:block}.metric strong{font-size:21px}.title{font-weight:900;font-size:18px;margin-bottom:10px}.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}label{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}input,select{width:100%;font-size:16px;padding:11px;border:1px solid #cbd6e2;border-radius:10px;background:#fff}button,.btn{border:0;border-radius:10px;background:var(--blue);color:#fff;padding:11px 13px;font-weight:800;text-decoration:none;display:inline-block;font-size:14px}.secondary{background:#edf2f7;color:#26384d}.green{background:var(--green)}.red{background:var(--red)}.gold{background:var(--gold)}.actions{display:flex;gap:7px;flex-wrap:wrap}.note,.ok,.bad{padding:11px;border-radius:11px;margin-bottom:10px;font-size:13px;line-height:1.6}.note{background:#fff7e5;border:1px solid #efd196;color:#704600}.ok{background:#eaf8ef;border:1px solid #a9d9b9;color:#155d31}.bad{background:#fff0ef;border:1px solid #efbbb5;color:#7d2118}.grade{font-size:42px;font-weight:950}.score{font-size:18px;font-weight:800;color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid #e5ebf1;text-align:left}.scroll{overflow:auto}.horse-card{border:1px solid #d7e2ee;border-radius:18px;padding:14px;margin-bottom:12px}.horse-no{font-size:30px;font-weight:950}.horse-name{font-size:20px;font-weight:900}.pick-grid,.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}.pick-grid>div,.stats-grid>div{background:#f5f8fb;border-radius:12px;padding:10px}.pick-grid span,.stats-grid span{display:block;color:var(--muted);font-size:12px}.pick-grid strong,.stats-grid strong{display:block;font-size:18px}.member-status{margin:8px 0 12px;padding:8px 12px;border-radius:10px;background:#eef6ff;color:#375a7f;font-size:13px}.member-status.setup{background:#fff8e8;color:#775112}.small{font-size:12px;color:var(--muted)}
@media(max-width:760px){.wrap{padding:10px}.head h1{font-size:27px}.nav{display:grid;grid-template-columns:1fr 1fr}.nav .btn{text-align:center;min-height:52px;display:flex;align-items:center;justify-content:center}.grid,.pick-grid,.stats-grid{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}.metric strong{font-size:18px}.desktop{display:none}.horse-no{font-size:28px}}

.course-block{background:#fff;border:1px solid var(--line);border-radius:16px;padding:13px;margin-bottom:10px}
.course-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}
.course-name{font-size:20px;font-weight:950}.race-links{display:flex;gap:6px;flex-wrap:wrap}
.batch-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.batch-card{border:1px solid #d8e2ec;border-radius:15px;padding:12px;background:#fff}
.batch-card .race-title{font-size:18px;font-weight:900}
.closing-hero{border:2px solid #7db795;background:#f1fbf5;border-radius:18px;padding:14px;margin-bottom:12px}
.closing-count{font-size:24px;font-weight:950}
.validation-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.validation-grid>div{background:#f5f8fb;border-radius:12px;padding:10px}
.hero{background:linear-gradient(135deg,#eaf8ef,#eef6ff);border-radius:18px;padding:16px;margin-bottom:10px}
.quick-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px}
.quick{background:#fff;border:1px solid var(--line);border-radius:15px;padding:14px;text-decoration:none;color:var(--ink)}
.quick strong{display:block;font-size:18px;margin-bottom:4px}
@media(max-width:760px){.batch-grid,.quick-grid,.validation-grid{grid-template-columns:1fr}.course-head{flex-direction:column;align-items:stretch}}
"""


def page(body,title=APP_TITLE):
    member=(f'<div class="member-status">会員ログイン中：{html.escape(str(session.get("member_id","")))}　<a href="/logout">ログアウト</a></div>' if LOGIN_ENABLED and session.get("member_authenticated") else ('<div class="member-status setup">販売前：会員ログイン未設定</div>' if not LOGIN_ENABLED else ''))
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="地方競馬1頭勝負"><title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class="wrap"><div class="head"><h1>{APP_TITLE}</h1><span class="badge">単勝＋複勝</span></div><div class="nav"><a class="btn secondary" href="/">ホーム</a><a class="btn secondary" href="/analyze">1頭勝負予想</a><a class="btn secondary" href="/picks">今日の本命</a><a class="btn secondary" href="/history">成績履歴</a><a class="btn secondary" href="/analytics">成績分析</a><a class="btn secondary" href="/validation">予想検証</a><a class="btn secondary" href="/courses">本日の開催</a><a class="btn green" href="/closing-soon">発走5分前</a></div>{member}{body}<div class="note">このv3は市場オッズ中心のルールベース参考評価です。的中・利益を保証しません。実際の投票・最終確認は公式投票サイトでご自身で行ってください。</div></div></body></html>'''


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
        draft=f'''<div class="card"><div class="title">現在の本命1頭</div><div class="horse-card"><div class="horse-no">{d.get('horse_no','')}番</div><div class="horse-name">{html.escape(str(d.get('horse_name','')))}</div><div class="pick-grid"><div><span>複勝オッズ</span><strong>{float(d.get('place_low') or 0):.1f}～{float(d.get('place_high') or 0):.1f}倍</strong></div><div><span>判定</span><strong>{html.escape(str(d.get('grade','')))}</strong></div><div><span>参考EV</span><strong>{float(d.get('ev_index') or 0):.2f}</strong></div><div><span>買い方</span><strong>単勝100円＋複勝200円</strong></div></div></div><form method="post" action="/record"><button class="green">この1頭を購入記録へ</button></form></div>'''
    return page(f'''{msg_html}
<div class="hero"><div class="title">単勝100円＋複勝200円・1頭勝負</div>
<div>市場オッズを主役に、近走・競馬場・距離適性を控えめに加えた第一段階です。</div></div>
<div class="quick-grid">
<a class="quick" href="/courses"><strong>🏇 本日の開催</strong><span>競馬場ごとに全レース一括予想</span></a>
<a class="quick" href="/closing-soon"><strong>⏱ 発走5分前</strong><span>発走が近いレースだけ抽出</span></a>
<a class="quick" href="/validation"><strong>📊 予想検証</strong><span>NAR公式結果で自動採点</span></a>
</div><div class="grid"><div class="card metric"><small>本日の上限</small><strong>{DAILY_LIMIT:,}円</strong></div><div class="card metric"><small>使用額</small><strong>{s['bet']:,}円</strong></div><div class="card metric"><small>残り予算</small><strong>{s['remaining']:,}円</strong></div><div class="card metric"><small>本日の収支</small><strong>{s['profit']:+,}円</strong></div></div><div class="card"><div class="title">単勝100円＋複勝200円・1頭勝負</div><div class="actions"><a class="btn green" href="/analyze">オッズ取得 → 本命1頭予想</a><a class="btn gold" href="/picks">今日の本命を見る</a><a class="btn secondary" href="https://www.spat4.jp/keiba/pc" target="_blank" rel="noopener">SPAT4公式サイトを開く</a></div></div>{draft}''')

@app.route("/analyze",methods=["GET","POST"])
def analyze():
    course=request.values.get("course",""); race=to_int(request.values.get("race",""),0)
    opts=''.join(f'<option {"selected" if c==course else ""}>{c}</option>' for c in NAR_COURSE_CODES)
    ropts=''.join(f'<option value="{n}" {"selected" if n==race else ""}>{n}R</option>' for n in range(1,13))
    form=f'''<div class="card"><div class="title">複勝オッズ取得 → 本命1頭予想</div><form method="post"><div class="two"><div><label>競馬場</label><select name="course"><option value="">選択</option>{opts}</select></div><div><label>レース</label><select name="race"><option value="">選択</option>{ropts}</select></div></div><br><button class="green">本命1頭を分析</button></form></div>'''
    if request.method=="GET" and request.args.get("auto")!="1": return page(form,"複勝1頭予想")
    if course not in NAR_COURSE_CODES or not 1<=race<=12: return page(form+'<div class="bad">競馬場とレースを選んでください。</div>')
    try: horses=nar_get_horses(course,race)
    except Exception as e: return page(form+f'<div class="bad">取得エラー：{html.escape(type(e).__name__)} - {html.escape(str(e))}</div>')
    if not horses: return page(form+'<div class="note">単勝・複勝オッズを取得できませんでした。発売前・締切後・更新中の可能性があります。</div>')
    try:
        form_data=nar_get_form_data(course,race,horses)
    except Exception:
        form_data={}
    remaining=summary()["remaining"]; result=evaluate(horses,remaining,form_data); save_pick(course,race,result); save_validation_prediction(course,race,result,remaining); recs=result["recs"]
    reasons=''.join(f'<li>{html.escape(x)}</li>' for x in result["reasons"])
    cards=''
    for i,x in enumerate(recs,1):
        cards+=f'''<div class="horse-card"><div><strong>{i}位候補</strong></div><div class="horse-no">{x['horse_no']}番</div><div class="horse-name">{html.escape(x['horse_name'])}</div><div class="pick-grid"><div><span>単勝</span><strong>{x['win_odds']:.1f}倍</strong></div><div><span>複勝</span><strong>{x['place_low']:.1f}～{x['place_high']:.1f}倍</strong></div><div><span>候補評価</span><strong>{x['confidence']}点</strong></div><div><span>優先度</span><strong>{x['priority_score']:.1f}</strong></div><div><span>参考EV</span><strong>{x['ev_index']:.2f}</strong><span>{x['ev_label']}</span></div><div><span>実績評価</span><strong>{x.get('form_rating',50):.1f}点</strong></div><div><span>従来優先度</span><strong>{x.get('base_priority_score',x['priority_score']):.1f}</strong></div></div></div>'''
    button=''
    if recs:
        b=recs[0]; amount=recommended_amount(result["grade"],summary()["remaining"],b["place_low"])
        button=f'''<form method="post" action="/apply"><input type="hidden" name="course" value="{html.escape(course)}"><input type="hidden" name="race" value="{race}"><input type="hidden" name="horse_no" value="{b['horse_no']}"><input type="hidden" name="horse_name" value="{html.escape(b['horse_name'])}"><input type="hidden" name="place_low" value="{b['place_low']}"><input type="hidden" name="place_high" value="{b['place_high']}"><input type="hidden" name="grade" value="{result['grade']}"><input type="hidden" name="score" value="{result['score']}"><input type="hidden" name="ev_index" value="{b['ev_index']}"><input type="hidden" name="amount" value="{amount}"><button class="green">単勝100円＋複勝200円をホームへ入力（合計{amount:,}円）</button></form>'''
    return page(form+f'''<div class="card"><div class="title">{html.escape(course)} {race}R 参考判定</div><div class="grade">{result['grade']}</div><div class="score">参考スコア {result['score']} / 100</div><ul>{reasons}</ul><div class="small">※参考EVは実際の的中確率ではありません。市場オッズを主役に、近走・競馬場・距離適性を控えめに補正しています。S/Aのみ購入候補、Bは観察用です。買い方は単勝100円＋複勝200円です。</div></div><div class="card"><div class="title">本命1頭</div>{cards}{button}</div>''')

@app.post("/apply")
def apply():
    item={"horse_no":to_int(request.form.get("horse_no")),"horse_name":request.form.get("horse_name",""),"place_low":to_float(request.form.get("place_low")),"place_high":to_float(request.form.get("place_high")),"ev_index":to_float(request.form.get("ev_index"))}
    save_draft(item,request.form.get("course",""),to_int(request.form.get("race")),request.form.get("grade","見送り"),to_int(request.form.get("score")),to_int(request.form.get("amount")))
    return redirect(url_for("home",msg="本命1頭をホームへ入力しました。"))

@app.post("/record")
def record():
    d=get_draft()
    if not d: return redirect(url_for("home",msg="先に本命1頭を分析してください。"))
    amount=int(d.get("amount") or 0)
    if amount<100: return redirect(url_for("home",msg="見送り判定のため推奨購入額は0円です。"))
    if amount>summary()["remaining"]: return redirect(url_for("home",msg="本日の残り予算を超えています。"))
    with db() as con:
        con.execute("""INSERT INTO purchases(created_at,race_date,course,race,horse_no,horse_name,place_low,place_high,grade,score,ev_index,amount,result,return_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now().strftime("%Y-%m-%d %H:%M:%S"),today(),d["course"],d["race"],d["horse_no"],d["horse_name"],d["place_low"],d["place_high"],d["grade"],d["score"],d["ev_index"],amount,"未確定",0))
    return redirect(url_for("history",msg="購入記録を追加しました。レース後に結果を入力してください。"))

@app.get("/picks")
def picks():
    with db() as con: rows=con.execute("SELECT * FROM picks WHERE race_date=? ORDER BY CASE grade WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 ELSE 9 END,score DESC",(today(),)).fetchall()
    body=''.join(f'''<div class="horse-card"><div class="horse-name">{html.escape(r['course'])} {html.escape(r['race'])}　{r['horse_no']}番 {html.escape(r['horse_name'])}</div><div class="pick-grid"><div><span>判定</span><strong>{r['grade']}</strong></div><div><span>スコア</span><strong>{r['score']}</strong></div><div><span>複勝</span><strong>{r['place_low']:.1f}～{r['place_high']:.1f}</strong></div><div><span>参考EV</span><strong>{r['ev_index']:.2f}</strong></div></div></div>''' for r in rows) or '<div class="note">本日の分析済み本命はまだありません。</div>'
    return page(f'<div class="card"><div class="title">今日の本命1頭</div>{body}</div>')


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
    return page(mh+f'<div class="card"><form method="post" action="/history/auto-results"><button class="green">NAR公式から未確定結果を自動取得</button></form></div>'+f'''<div class="card"><div class="title">通算成績</div><div class="stats-grid"><div><span>確定</span><strong>{s['n']}R</strong></div><div><span>的中率</span><strong>{s['hit_rate']:.1f}%</strong></div><div><span>回収率</span><strong>{s['roi']:.1f}%</strong></div><div><span>収支</span><strong>{s['profit']:+,}円</strong></div></div></div><div class="card"><div class="title">成績履歴</div>{cards or '<div class="note">購入記録はまだありません。</div>'}</div>''')


@app.post("/history/auto-results")
def history_auto_results():
    with db() as con:
        rows=con.execute("SELECT * FROM purchases WHERE result='未確定' ORDER BY id").fetchall()
    updated=pending=0
    for r in rows:
        race_no=to_int(re.sub(r"\D","",r["race"]),0)
        settled=settle_tanfuku(
            r["horse_no"],
            nar_get_tanfuku_refunds(r["course"],race_no,r["race_date"])
        )
        if not settled:
            pending+=1
            continue
        with db() as con:
            con.execute(
                "UPDATE purchases SET result=?,return_amount=? WHERE id=?",
                (settled["result"],settled["return_amount"],r["id"])
            )
        updated+=1
    return redirect(url_for("history",msg=f"NAR公式結果を確認しました。更新{updated}件 / 未確定{pending}件"))


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


@app.get("/courses")
def courses():
    blocks = ""
    active = []
    for c in NAR_COURSE_CODES:
        try:
            nums = race_numbers(c)
        except Exception:
            nums = []
        if not nums:
            continue
        active.append(c)
        links = "".join(
            f'<a class="btn secondary" href="/analyze?course={urllib.parse.quote(c)}&race={n}&auto=1">{n}R</a>'
            for n in nums
        )
        blocks += (
            '<div class="course-block">'
            '<div class="course-head">'
            f'<div class="course-name">{html.escape(c)}</div>'
            '<div class="actions">'
            f'<a class="btn secondary" href="/analyze?course={urllib.parse.quote(c)}&race={nums[-1]}&auto=1">最終Rを予想</a>'
            f'<a class="btn green" href="/course-batch?course={urllib.parse.quote(c)}">全レース一括予想</a>'
            '</div></div>'
            f'<div class="race-links">{links}</div>'
            '</div>'
        )
    allbtn = '<a class="btn gold" href="/all-batch">本日の全開催を一括予想</a>' if active else ''
    body = (
        f'<div class="card"><div class="title">本日の開催</div><div class="actions">{allbtn}</div></div>'
        + (blocks or '<div class="note">現在取得できる開催情報がありません。</div>')
    )
    return page(body)


def batch_predict_course(course, remaining):
    try:
        races = race_numbers(course)
    except Exception:
        races = []
    def worker(race_no):
        try:
            horses = nar_get_horses(course, race_no)
            if not horses:
                return race_no, "skip", "単勝・複勝未発売・取得不可", None
            try:
                form_data = nar_get_form_data(course, race_no, horses)
            except Exception:
                form_data = {}
            return race_no, "ok", "", evaluate(horses, remaining, form_data)
        except Exception as exc:
            return race_no, "error", f"{type(exc).__name__}: {exc}", None
    out = []
    if not races:
        return out
    with ThreadPoolExecutor(max_workers=min(4, len(races))) as pool:
        futures = [pool.submit(worker, r) for r in races]
        for f in as_completed(futures):
            out.append(f.result())
    out.sort(key=lambda x: x[0])
    return out


def render_batch_cards(course, rows, remaining):
    cards = ""
    for race_no, status, msg, result in rows:
        if status != "ok" or not result:
            cards += (
                f'<div class="batch-card"><div class="race-title">{race_no}R</div>'
                f'<div class="note">{html.escape(msg)}</div></div>'
            )
            continue
        save_pick(course, race_no, result)
        save_validation_prediction(course, race_no, result, remaining)
        b = result["recs"][0] if result["recs"] else None
        if not b:
            continue
        amount = recommended_amount(result["grade"], remaining, b["place_low"])
        cards += (
            '<div class="batch-card">'
            f'<div class="race-title">{race_no}R　{result["grade"]} / {result["score"]}点</div>'
            f'<div class="horse-name">{b["horse_no"]}番 {html.escape(b["horse_name"])}</div>'
            '<div class="pick-grid">'
            f'<div><span>単勝</span><strong>{b["win_odds"]:.1f}倍</strong></div>'
            f'<div><span>複勝</span><strong>{b["place_low"]:.1f}～{b["place_high"]:.1f}</strong></div>'
            f'<div><span>参考EV</span><strong>{b["ev_index"]:.2f}</strong></div>'
            f'<div><span>推奨額</span><strong>{amount:,}円</strong></div>'
            f'<div><span>実績評価</span><strong>{b.get("form_rating",50):.1f}点</strong></div>'
            '</div>'
            f'<div class="actions" style="margin-top:8px"><a class="btn secondary" href="/analyze?course={urllib.parse.quote(course)}&race={race_no}&auto=1">詳しく見る</a></div>'
            '</div>'
        )
    return cards


@app.get("/course-batch")
def course_batch():
    course = request.args.get("course", "").strip()
    if course not in NAR_COURSE_CODES:
        return page('<div class="bad">競馬場を選択してください。</div>')
    remaining = summary()["remaining"]
    rows = batch_predict_course(course, remaining)
    body = (
        f'<div class="card"><div class="title">{html.escape(course)} 全レース一括予想</div>'
        '<div class="small">現在の単複予想ロジックは変更していません。</div></div>'
        f'<div class="batch-grid">{render_batch_cards(course, rows, remaining)}</div>'
    )
    return page(body)


@app.get("/all-batch")
def all_batch():
    remaining = summary()["remaining"]
    body = ""
    for course in NAR_COURSE_CODES:
        rows = batch_predict_course(course, remaining)
        if rows:
            body += (
                f'<div class="card"><div class="title">{html.escape(course)}</div>'
                f'<div class="batch-grid">{render_batch_cards(course, rows, remaining)}</div></div>'
            )
    return page(body or '<div class="note">一括予想できる開催がありません。</div>')


@app.get("/closing-soon")
def closing_soon():
    active, future = closing_soon_candidates(5)
    remaining = summary()["remaining"]
    if not active:
        nxt = ""
        if future:
            x = future[0]
            nxt = (
                f'<div class="note">次に近いレース：{html.escape(x["course"])} {x["race"]}R'
                f'　発走予定 {x["start_dt"].strftime("%H:%M")}</div>'
            )
        body = (
            '<div class="card"><div class="title">発走5分前レース</div>'
            '<div class="closing-hero"><div class="closing-count">今は対象レースがありません</div>'
            f'<div>現在時刻 {now().strftime("%H:%M")}</div></div>{nxt}</div>'
        )
        return page(body)

    cards = ""
    for x in active:
        try:
            horses = nar_get_horses(x["course"], x["race"])
        except Exception:
            horses = []
        if not horses:
            continue
        try:
            form_data = nar_get_form_data(x["course"], x["race"], horses)
        except Exception:
            form_data = {}
        result = evaluate(horses, remaining, form_data)
        save_pick(x["course"], x["race"], result)
        save_validation_prediction(x["course"], x["race"], result, remaining)
        b = result["recs"][0] if result["recs"] else None
        if not b:
            continue
        sec = max(0, int((x["start_dt"] - now()).total_seconds()))
        cards += (
            '<div class="horse-card">'
            f'<div class="horse-name">{html.escape(x["course"])} {x["race"]}R'
            f'　発走 {x["start_dt"].strftime("%H:%M")}　残り約{sec//60}分{sec%60:02d}秒</div>'
            f'<div class="grade">{result["grade"]}</div>'
            f'<div>{b["horse_no"]}番 {html.escape(b["horse_name"])}</div>'
            f'<div class="actions" style="margin-top:8px"><a class="btn green" href="/analyze?course={urllib.parse.quote(x["course"])}&race={x["race"]}&auto=1">詳しく見る</a></div>'
            '</div>'
        )
    return page(f'<div class="card"><div class="title">発走5分前レース</div>{cards}</div>')


@app.get("/validation")
def validation():
    with db() as con:
        rows = con.execute(
            "SELECT * FROM validation_predictions ORDER BY race_date DESC,id DESC LIMIT 300"
        ).fetchall()
    settled = [r for r in rows if r["result"] in ("的中", "ハズレ")]
    n = len(settled)
    hits = sum(1 for r in settled if r["result"] == "的中")
    bet = sum(int(r["amount"] or 0) for r in settled)
    ret = sum(int(r["return_amount"] or 0) for r in settled)
    roi = ret / bet * 100 if bet else 0

    cards = ""
    for r in rows:
        cards += (
            '<div class="horse-card">'
            f'<div class="horse-name">{r["race_date"]}　{html.escape(r["course"])} {html.escape(r["race"])}'
            f'　{r["horse_no"]}番 {html.escape(r["horse_name"])}</div>'
            '<div class="pick-grid">'
            f'<div><span>グレード</span><strong>{r["grade"]}</strong></div>'
            f'<div><span>スコア</span><strong>{r["score"]}</strong></div>'
            f'<div><span>複勝</span><strong>{r["place_low"]:.1f}～{r["place_high"]:.1f}</strong></div>'
            f'<div><span>参考EV</span><strong>{r["ev_index"]:.2f}</strong></div>'
            '</div>'
            f'<div class="small" style="margin-top:7px">結果：{r["result"]} ／ 払戻：{int(r["return_amount"] or 0):,}円 ／ 検証購入額：{int(r["amount"] or 0):,}円</div>'
            '</div>'
        )

    body = (
        '<div class="card"><div class="title">予想検証ダッシュボード</div>'
        '<form method="post" action="/validation/auto-results"><button class="green">NAR公式から未確定結果を自動取得</button></form>'
        '<div class="validation-grid" style="margin-top:10px">'
        f'<div>記録数<br><strong>{len(rows)}</strong></div>'
        f'<div>確定数<br><strong>{n}</strong></div>'
        f'<div>的中率<br><strong>{hits/n*100 if n else 0:.1f}%</strong></div>'
        f'<div>回収率<br><strong>{roi:.1f}%</strong></div>'
        '</div>'
        f'<div class="ok">検証収支 {ret-bet:+,}円 ／ 購入額 {bet:,}円 ／ 払戻 {ret:,}円</div>'
        '</div>'
        + cards
    )
    return page(body)


@app.post("/validation/auto-results")
def validation_auto_results():
    with db() as con:
        rows = con.execute(
            "SELECT * FROM validation_predictions WHERE result='未確定'"
        ).fetchall()
    updated = 0
    pending = 0
    for r in rows:
        race_no = to_int(re.sub(r"\D", "", r["race"]), 0)
        settled = settle_tanfuku(
            r["horse_no"],
            nar_get_tanfuku_refunds(r["course"], race_no, r["race_date"])
        )
        if not settled:
            pending += 1
            continue
        with db() as con:
            con.execute(
                """UPDATE validation_predictions
                SET result=?,return_amount=?,official_result=?,checked_at=?,result_source='NAR公式'
                WHERE id=?""",
                (
                    settled["result"], settled["return_amount"], settled["official_result"],
                    now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]
                )
            )
        updated += 1
    return redirect(url_for("validation", updated=updated, pending=pending))



@app.get("/health")
def health(): return "ok",200

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),debug=True)
