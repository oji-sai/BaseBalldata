#!/usr/bin/env python3
"""
野球ダッシュボード（個人用）
============================
タブ「ニュース全体 / デトロイト / 阪神」を切り替えると、各チームの
  ・直近スコア
  ・1軍の好調選手（セイバー指標で並べる）
  ・2軍/マイナーの好調選手（デトロイトは階層ラベル付き）
  ・ニュース
がまとまったHTML(baseball_dashboard.html)を生成します。完全ローカル・個人用。

データ元:
  デトロイト … MLB StatsAPI（公式・無料・キー不要） statsapi.mlb.com
                1軍打者は wOBA、投手は FIP を「実際に計算」して並べる
                （StatsAPIは wOBA/FIP を持たないので、構成要素から固定ウェイトで算出＝近似）
  阪神        … 成績は baseball-data.com（プロ野球データFreak）をスクレイピングし OPS 降順
                ニュースは Googleニュース RSS（日本語・翻訳不要）

【依存】
  pip install feedparser requests pandas lxml

【使い方】
  1. （デトロイトのニュース翻訳を使うなら）DEEPL_API_KEY を設定
  2. python baseball_dashboard.py
  3. 生成された baseball_dashboard.html をブラウザで開く

※このスクリプトはネット上の各データ元にアクセスして動きます。実行環境から
  statsapi.mlb.com / news.google.com / baseball-data.com に到達できる必要があります。
※阪神のスクレイピングはサイト構造に依存します。列が取れないときは、その旨と
  「実際の列名一覧」をコンソールに出すので、それを見て COLUMN 設定を直してください。
"""

import html
import os
import re
import sys
from datetime import date, datetime, timedelta
from urllib.parse import quote

import feedparser
import requests

# ====== 設定 =======================================================

SEASON = 2026
# 環境変数 DEEPL_API_KEY があればそれを使う（GitHub ActionsのSecrets用）。
# ローカルで使うなら下の "ここに..." を書き換えてもOK。
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "ここにDeepLのAPIキーを貼る")

MLB_TEAM_ID = 116          # Detroit Tigers
MLB_TEAM_LABEL = "デトロイト"
NPB_TEAM_CODE = "t"        # baseball-data.com のチーム記号（阪神=t）
NPB_TEAM_LABEL = "阪神"

TOP_N = 5                  # 各リストの表示件数
MIN_PA = 40               # 1軍打者の最低打席（少ないサンプルを除外）
MIN_IP = 20               # 1軍投手の最低投球回

HEADERS = {"User-Agent": "Mozilla/5.0 (personal-dashboard; local use)"}
DEEPL_URL = "https://api-free.deepl.com/v2/translate"

# マイナー階層（sportId: 表示名）
MILB_LEVELS = {11: "AAA", 12: "AA", 13: "A+", 14: "A"}

# 阪神ニュース＝Googleニュース検索、デトロイトニュース＝MLB公式RSS
NEWS_FEEDS = {
    NPB_TEAM_LABEL: {
        "url": f"https://news.google.com/rss/search?q={quote('阪神タイガース')}&hl=ja&gl=JP&ceid=JP:ja",
        "translate": False,
    },
    MLB_TEAM_LABEL: {
        "url": "https://www.mlb.com/tigers/feeds/news/rss.xml",
        "translate": True,
    },
}

# ===================================================================
#  共通ユーティリティ
# ===================================================================

def num(stat: dict, key: str) -> float:
    """stat dict から数値を安全に取り出す（文字列でも変換）。"""
    v = stat.get(key, 0)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def ip_to_float(ip) -> float:
    """'45.1' 形式の投球回（45と1/3）を実数に。"""
    try:
        whole, _, frac = str(ip).partition(".")
        return int(whole or 0) + {"": 0, "0": 0, "1": 1/3, "2": 2/3}.get(frac, 0)
    except Exception:
        return 0.0


def compute_woba(s: dict):
    bb = num(s, "baseOnBalls"); ibb = num(s, "intentionalWalks"); hbp = num(s, "hitByPitch")
    h = num(s, "hits"); d = num(s, "doubles"); t = num(s, "triples"); hr = num(s, "homeRuns")
    ab = num(s, "atBats"); sf = num(s, "sacFlies")
    singles = h - d - t - hr
    denom = ab + bb - ibb + sf + hbp
    if denom <= 0:
        return None
    # 固定ウェイト（年次で微変動する近似値）
    return round((0.69 * (bb - ibb) + 0.72 * hbp + 0.88 * singles
                  + 1.25 * d + 1.58 * t + 2.03 * hr) / denom, 3)


def compute_fip(s: dict):
    hr = num(s, "homeRuns"); bb = num(s, "baseOnBalls")
    hbp = num(s, "hitByPitch"); k = num(s, "strikeOuts")
    ip = ip_to_float(s.get("inningsPitched", "0"))
    if ip <= 0:
        return None
    return round((13 * hr + 3 * (bb + hbp) - 2 * k) / ip + 3.10, 2)  # 定数3.10は近似


def compute_kbb_pct(s: dict):
    """K-BB% = (奪三振 - 与四球) / 対戦打者数。"""
    bf = num(s, "battersFaced")
    if bf <= 0:
        return None
    return round((num(s, "strikeOuts") - num(s, "baseOnBalls")) / bf * 100, 1)


def compute_wrc_plus(woba, const):
    """パーク補正なし(PF=1)の近似 wRC+。const は mlb_league_constants() の戻り値。"""
    if woba is None or not const:
        return None
    lg = const.get("lgwOBA"); scale = const.get("wobaScale"); rpa = const.get("lgR_PA")
    if not lg or not rpa:
        return None
    wraa_pa = (woba - lg) / scale
    return round((1 + wraa_pa / rpa) * 100)


# ===================================================================
#  デトロイト（MLB StatsAPI）
# ===================================================================

def mlb_get(path: str, params: dict = None) -> dict:
    r = requests.get(f"https://statsapi.mlb.com/api/v1/{path}",
                     params=params or {}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def mlb_recent_scores(n=6):
    end = date.today(); start = end - timedelta(days=12)
    data = mlb_get("schedule", {
        "sportId": 1, "teamId": MLB_TEAM_ID,
        "startDate": start.isoformat(), "endDate": end.isoformat(),
    })
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            home = g["teams"]["home"]; away = g["teams"]["away"]
            games.append({
                "date": g.get("officialDate", ""),
                "away": away["team"]["name"], "away_score": away.get("score"),
                "home": home["team"]["name"], "home_score": home.get("score"),
                "status": g.get("status", {}).get("detailedState", ""),
            })
    return games[-n:][::-1]


def _mlb_roster(team_id):
    data = mlb_get(f"teams/{team_id}/roster", {"rosterType": "active"})
    return [(p["person"]["id"], p["person"]["fullName"]) for p in data.get("roster", [])]


def _mlb_player_stats(ids, group, team_id=None):
    """複数選手のシーズン成績をまとめて取得。 {playerId: statdict} を返す。"""
    if not ids:
        return {}
    out = {}
    # 一度に多いとURLが長くなるので分割
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        data = mlb_get("people", {
            "personIds": ",".join(map(str, chunk)),
            "hydrate": f"stats(group=[{group}],type=[season],season={SEASON},gameType=R)",
        })
        for person in data.get("people", []):
            picked = None
            for s in person.get("stats", []):
                for split in s.get("splits", []):
                    if team_id and split.get("team", {}).get("id") != team_id:
                        continue
                    picked = split.get("stat"); break
                if picked:
                    break
            if picked:
                out[person["id"]] = picked
    return out


def mlb_league_constants():
    """今季MLB全体の集計から リーグwOBA / リーグR/PA を出す（wRC+用）。"""
    agg = {k: 0.0 for k in ("baseOnBalls", "intentionalWalks", "hitByPitch", "hits",
                            "doubles", "triples", "homeRuns", "atBats", "sacFlies",
                            "runs", "plateAppearances")}
    teams = mlb_get("teams", {"sportId": 1, "season": SEASON}).get("teams", [])
    for t in teams:
        try:
            d = mlb_get(f"teams/{t['id']}/stats",
                        {"stats": "season", "group": "hitting", "season": SEASON})
            s = d["stats"][0]["splits"][0]["stat"]
            for k in agg:
                agg[k] += num(s, k)
        except Exception:
            continue
    pa = agg["plateAppearances"] or 1
    return {"lgwOBA": compute_woba(agg), "lgR_PA": agg["runs"] / pa, "wobaScale": 1.24}


def mlb_top_hitters(team_id, min_pa, top, level=None, const=None):
    roster = _mlb_roster(team_id)
    names = {pid: nm for pid, nm in roster}
    stats = _mlb_player_stats(list(names), "hitting", team_id)
    rows = []
    for pid, s in stats.items():
        if num(s, "plateAppearances") < min_pa:
            continue
        w = compute_woba(s)
        if w is None:
            continue
        rows.append({
            "name": names.get(pid, "?"), "level": level,
            "woba": w, "wrc": compute_wrc_plus(w, const),
            "ops": s.get("ops", "-"),
            "avg": s.get("avg", "-"), "hr": int(num(s, "homeRuns")),
        })
    # const があれば wRC+ 順、無ければ wOBA 順（マイナーは wOBA 順）
    key = "wrc" if const else "woba"
    rows.sort(key=lambda x: (x[key] if x[key] is not None else -999), reverse=True)
    return rows[:top]


def mlb_top_pitchers(team_id, min_ip, top):
    roster = _mlb_roster(team_id)
    names = {pid: nm for pid, nm in roster}
    stats = _mlb_player_stats(list(names), "pitching", team_id)
    rows = []
    for pid, s in stats.items():
        if ip_to_float(s.get("inningsPitched", "0")) < min_ip:
            continue
        f = compute_fip(s)
        if f is None:
            continue
        rows.append({
            "name": names.get(pid, "?"),
            "fip": f, "kbb": compute_kbb_pct(s),
            "era": s.get("era", "-"),
            "whip": s.get("whip", "-"), "so": int(num(s, "strikeOuts")),
        })
    rows.sort(key=lambda x: x["fip"])
    return rows[:top]


def mlb_minor_leaders():
    """組織傘下のマイナー各階層から、好調打者を1人ずつ（wOBA基準）。"""
    picks = []
    for sport_id, label in MILB_LEVELS.items():
        try:
            teams = mlb_get("teams", {"sportId": sport_id, "season": SEASON})
            affs = [t for t in teams.get("teams", []) if t.get("parentOrgId") == MLB_TEAM_ID]
            for aff in affs:
                top = mlb_top_hitters(aff["id"], min_pa=25, top=1, level=label)
                if top:
                    picks.append(top[0])
        except Exception as e:
            print(f"  [マイナー{label} 取得失敗] {e}", file=sys.stderr)
    # 階層順（AAA→A）で並べる
    order = {"AAA": 0, "AA": 1, "A+": 2, "A": 3}
    picks.sort(key=lambda x: order.get(x["level"], 9))
    return picks


# ===================================================================
#  阪神（スクレイピング）
# ===================================================================

def _read_tables(url):
    import pandas as pd
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return pd.read_html(r.text)


def _pick_hitter_rows(url, top):
    """baseball-data.com の打者テーブルから OPS 降順で上位を返す。"""
    tables = _read_tables(url)
    # 一番大きい表を採用
    df = max(tables, key=lambda d: d.shape[0] * d.shape[1]).copy()
    df.columns = [str(c).strip() for c in df.columns]

    name_col = next((c for c in df.columns if "選手" in c or "名前" in c), None)
    ops_col = next((c for c in df.columns if "OPS" in c.upper()), None)
    avg_col = next((c for c in df.columns if "打率" in c), None)
    hr_col = next((c for c in df.columns if "本塁打" in c or c == "HR"), None)

    if name_col is None or ops_col is None:
        raise ValueError(
            "打者テーブルの列を特定できませんでした。実際の列名: " + ", ".join(df.columns)
        )

    import pandas as pd
    df["_ops"] = pd.to_numeric(df[ops_col], errors="coerce")
    df = df.dropna(subset=["_ops"]).sort_values("_ops", ascending=False)
    rows = []
    for _, r in df.head(top).iterrows():
        rows.append({
            "name": str(r[name_col]),
            "ops": f'{r["_ops"]:.3f}',
            "avg": str(r[avg_col]) if avg_col else "-",
            "hr": str(r[hr_col]) if hr_col else "-",
        })
    return rows


def npb_top_hitters_ichigun(top):
    return _pick_hitter_rows(f"https://baseball-data.com/stats/hitter-{NPB_TEAM_CODE}/", top)


def npb_farm_wrc(top):
    """farm-stats.com（2軍成績.com）から阪神ファーム打者を wRC+ 降順で取得。
    失敗したら baseball-data.com の OPS 版にフォールバック。"""
    import pandas as pd
    try:
        tables = _read_tables("https://www.farm-stats.com/stats/hitter")
        df = max(tables, key=lambda d: d.shape[0] * d.shape[1]).copy()
        df.columns = [str(c).strip() for c in df.columns]

        name_col = next((c for c in df.columns if "選手" in c or "名前" in c), None)
        team_col = next((c for c in df.columns if "チーム" in c or "球団" in c), None)
        wrc_col = next((c for c in df.columns if "wRC" in c or "WRC" in c.upper()), None)
        ops_col = next((c for c in df.columns if "OPS" in c.upper()), None)
        if name_col is None or wrc_col is None:
            raise ValueError("farm-stats 列を特定できません。実際の列名: " + ", ".join(df.columns))

        if team_col is not None:  # 阪神だけに絞る
            df = df[df[team_col].astype(str).str.contains("阪神|Hanshin|タイガース", na=False)]
        df["_wrc"] = pd.to_numeric(df[wrc_col], errors="coerce")
        df = df.dropna(subset=["_wrc"]).sort_values("_wrc", ascending=False)
        rows = []
        for _, r in df.head(top).iterrows():
            rows.append({
                "name": str(r[name_col]),
                "wrc": int(r["_wrc"]),
                "ops": str(r[ops_col]) if ops_col else "-",
            })
        if rows:
            return rows
        raise ValueError("阪神のファーム打者が抽出できませんでした")
    except Exception as e:
        print(f"  [farm-stats wRC+ 失敗→OPS版にフォールバック] {e}", file=sys.stderr)
        return _pick_hitter_rows(
            f"https://baseball-data.com/stats-farm/hitter-{NPB_TEAM_CODE}/", top)


# ===================================================================
#  ニュース（両チーム共通）
# ===================================================================

def translate(text: str) -> str:
    text = (text or "").strip()
    if not text or "ここにDeepL" in DEEPL_API_KEY:
        return text
    try:
        resp = requests.post(DEEPL_URL, data={
            "auth_key": DEEPL_API_KEY, "text": text,
            "source_lang": "EN", "target_lang": "JA",
        }, timeout=20)
        resp.raise_for_status()
        return resp.json()["translations"][0]["text"]
    except Exception as e:
        print(f"  [翻訳失敗] {e}", file=sys.stderr)
        return text


def clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def collect_news(limit=12):
    news = {}
    for team, feed in NEWS_FEEDS.items():
        items = []
        try:
            parsed = feedparser.parse(feed["url"])
            for e in parsed.entries[:limit]:
                title = e.get("title", "")
                if feed["translate"]:
                    title = translate(title)
                items.append({"title": title, "link": e.get("link", ""),
                              "published": e.get("published", "")})
        except Exception as ex:
            print(f"  [{team} ニュース取得失敗] {ex}", file=sys.stderr)
        news[team] = items
    return news


# ===================================================================
#  HTML 生成
# ===================================================================

def _safe(fn, label):
    """パネル用データ取得を安全に。失敗時は ('__error__', メッセージ) を返す。"""
    try:
        return fn()
    except Exception as e:
        print(f"  [{label} 失敗] {e}", file=sys.stderr)
        return ("__error__", str(e))


def _scores_html(scores):
    if isinstance(scores, tuple):
        return f'<p class="err">スコア取得失敗: {html.escape(scores[1])}</p>'
    if not scores:
        return '<p class="muted">直近の試合が見つかりません。</p>'
    out = ['<table><tr><th>日付</th><th>対戦</th><th>結果</th><th></th></tr>']
    for g in scores:
        aw = "" if g["away_score"] is None else g["away_score"]
        hm = "" if g["home_score"] is None else g["home_score"]
        out.append(
            f'<tr><td>{html.escape(g["date"])}</td>'
            f'<td>{html.escape(g["away"])} @ {html.escape(g["home"])}</td>'
            f'<td>{aw} - {hm}</td><td class="muted">{html.escape(g["status"])}</td></tr>'
        )
    out.append("</table>")
    return "".join(out)


def _hitters_html(rows, show_level=False):
    if isinstance(rows, tuple):
        return f'<p class="err">取得失敗: {html.escape(rows[1])}</p>'
    if not rows:
        return '<p class="muted">データなし。</p>'
    if show_level:  # マイナー: 階層 + OPS + wOBA
        head = "<tr><th>選手</th><th>階層</th><th>OPS</th><th>wOBA</th><th>HR</th></tr>"
    else:           # 1軍: OPS + wRC+
        head = "<tr><th>選手</th><th>OPS</th><th>wRC+</th><th>打率</th><th>HR</th></tr>"
    body = []
    for r in rows:
        if show_level:
            body.append(
                f'<tr><td>{html.escape(str(r["name"]))}</td>'
                f'<td>{html.escape(str(r.get("level","")))}</td>'
                f'<td>{html.escape(str(r.get("ops","-")))}</td>'
                f'<td>{r.get("woba","-")}</td>'
                f'<td>{html.escape(str(r.get("hr","-")))}</td></tr>'
            )
        else:
            wrc = r.get("wrc"); wrc = "-" if wrc is None else wrc
            body.append(
                f'<tr><td>{html.escape(str(r["name"]))}</td>'
                f'<td>{html.escape(str(r.get("ops","-")))}</td>'
                f'<td>{wrc}</td>'
                f'<td>{html.escape(str(r.get("avg","-")))}</td>'
                f'<td>{html.escape(str(r.get("hr","-")))}</td></tr>'
            )
    return f"<table>{head}{''.join(body)}</table>"


def _hitters_html_ops(rows):
    """阪神用（wOBAなし・OPS基準）。"""
    if isinstance(rows, tuple):
        return f'<p class="err">取得失敗: {html.escape(rows[1])}</p>'
    if not rows:
        return '<p class="muted">データなし。</p>'
    body = ["<tr><th>選手</th><th>OPS</th><th>打率</th><th>HR</th></tr>"]
    for r in rows:
        body.append(
            f'<tr><td>{html.escape(str(r["name"]))}</td>'
            f'<td>{html.escape(str(r["ops"]))}</td>'
            f'<td>{html.escape(str(r.get("avg","-")))}</td>'
            f'<td>{html.escape(str(r.get("hr","-")))}</td></tr>'
        )
    return f"<table>{''.join(body)}</table>"


def _farm_wrc_html(rows):
    """阪神2軍用。wRC+があれば wRC+/OPS、無ければ OPS 版にフォールバック表示。"""
    if isinstance(rows, tuple):
        return f'<p class="err">取得失敗: {html.escape(rows[1])}</p>'
    if not rows:
        return '<p class="muted">データなし。</p>'
    if "wrc" not in rows[0]:            # OPS版フォールバックが来た場合
        return _hitters_html_ops(rows)
    body = ["<tr><th>選手</th><th>wRC+</th><th>OPS</th></tr>"]
    for r in rows:
        body.append(
            f'<tr><td>{html.escape(str(r["name"]))}</td>'
            f'<td>{r.get("wrc","-")}</td>'
            f'<td>{html.escape(str(r.get("ops","-")))}</td></tr>'
        )
    return f"<table>{''.join(body)}</table>"


def _pitchers_html(rows):
    if isinstance(rows, tuple):
        return f'<p class="err">取得失敗: {html.escape(rows[1])}</p>'
    if not rows:
        return '<p class="muted">データなし。</p>'
    body = ["<tr><th>投手</th><th>FIP</th><th>K-BB%</th><th>ERA</th><th>WHIP</th></tr>"]
    for r in rows:
        kbb = r.get("kbb"); kbb = "-" if kbb is None else f"{kbb}%"
        body.append(
            f'<tr><td>{html.escape(str(r["name"]))}</td><td>{r["fip"]}</td>'
            f'<td>{kbb}</td>'
            f'<td>{html.escape(str(r["era"]))}</td><td>{html.escape(str(r["whip"]))}</td></tr>'
        )
    return f"<table>{''.join(body)}</table>"


def _news_html(items):
    if not items:
        return '<p class="muted">ニュースなし。</p>'
    out = []
    for it in items:
        out.append(
            f'<div class="ni"><a href="{html.escape(it["link"])}" target="_blank">'
            f'{html.escape(it["title"])}</a>'
            f'<div class="muted">{html.escape(it["published"])}</div></div>'
        )
    return "".join(out)


def build_html(det, han, news):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # デトロイト・ダッシュボード
    det_html = f"""
    <div class="dashboard">
      <div class="left">
        <section class="scores"><h3>直近のスコア</h3>{_scores_html(det['scores'])}</section>
        <section class="r1"><h3>1軍・好調打者（wRC+順）</h3>{_hitters_html(det['hitters'])}
          <h3 style="margin-top:14px">1軍・好調投手（FIP順）</h3>{_pitchers_html(det['pitchers'])}</section>
        <section class="r2"><h3>マイナー・注目打者（階層別）</h3>{_hitters_html(det['minors'], show_level=True)}</section>
      </div>
      <aside class="news"><h3>ニュース</h3>{_news_html(news.get(MLB_TEAM_LABEL, []))}</aside>
    </div>"""

    # 阪神・ダッシュボード（wOBAなし・OPS基準、スコアはv1ではリンク）
    han_html = f"""
    <div class="dashboard">
      <div class="left">
        <section class="scores"><h3>直近のスコア</h3>
          <p class="muted">阪神のスコア自動取得は調整中です。
          <a href="https://npb.jp/" target="_blank">NPB公式</a> /
          <a href="https://baseball-data.com/team/" target="_blank">データFreak</a> で確認できます。</p></section>
        <section class="r1"><h3>1軍・好調打者（OPS順）</h3>{_hitters_html_ops(han['ichigun'])}</section>
        <section class="r2"><h3>2軍・好調打者（wRC+順）</h3>{_farm_wrc_html(han['farm'])}</section>
      </div>
      <aside class="news"><h3>ニュース</h3>{_news_html(news.get(NPB_TEAM_LABEL, []))}</aside>
    </div>"""

    # 全体タブ＝両チームのニュースをまとめて
    all_news = []
    for team, items in news.items():
        for it in items:
            all_news.append({**it, "title": f"[{team}] {it['title']}"})
    all_html = f'<div class="allnews"><h3>ニュース全体</h3>{_news_html(all_news)}</div>'

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>野球ダッシュボード</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, "Hiragino Sans", sans-serif;
         max-width: 1080px; margin: 0 auto; padding: 18px; line-height: 1.6; }}
  h1 {{ color:#c8102e; margin:0 0 2px; }} h3 {{ margin:0 0 8px; font-size:1rem; }}
  .updated {{ color:#888; font-size:.85rem; margin-bottom:12px; }}
  .tabs {{ display:flex; gap:4px; border-bottom:2px solid #c8102e; margin-bottom:14px; flex-wrap:wrap; }}
  .tab {{ padding:9px 18px; border:1px solid #ccc; border-bottom:none; background:#eee9;
         cursor:pointer; border-radius:6px 6px 0 0; }}
  .tab.active {{ background:#c8102e; color:#fff; border-color:#c8102e; }}
  .panel {{ display:none; }} .panel.active {{ display:block; }}
  .dashboard {{ display:grid; grid-template-columns:2fr 1fr; gap:12px; align-items:start; }}
  .left {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .left .scores {{ grid-column:1 / span 2; }}
  section, aside {{ border:1px solid #ddd8; border-radius:8px; padding:12px; background:#f7f7f733; }}
  table {{ width:100%; border-collapse:collapse; font-size:.86rem; }}
  th, td {{ text-align:left; padding:4px 6px; border-bottom:1px solid #ddd6; }}
  th {{ color:#888; font-weight:600; }}
  .ni {{ padding:6px 0; border-bottom:1px solid #eee5; font-size:.9rem; }}
  .ni a {{ text-decoration:none; }}
  .muted {{ color:#999; font-size:.82rem; }} .err {{ color:#c0392b; font-size:.82rem; }}
  @media (max-width:760px) {{
    .dashboard {{ grid-template-columns:1fr; }} .left {{ grid-template-columns:1fr; }}
    .left .scores {{ grid-column:1; }}
  }}
</style></head>
<body>
  <h1>⚾ 野球ダッシュボード</h1>
  <div class="updated">最終更新: {now}</div>
  <div class="tabs">
    <button class="tab active" data-t="all">ニュース全体</button>
    <button class="tab" data-t="det">{MLB_TEAM_LABEL}</button>
    <button class="tab" data-t="han">{NPB_TEAM_LABEL}</button>
  </div>
  <div class="panel active" id="all">{all_html}</div>
  <div class="panel" id="det">{det_html}</div>
  <div class="panel" id="han">{han_html}</div>
<script>
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.panel');
  tabs.forEach(b => b.addEventListener('click', () => {{
    tabs.forEach(x => x.classList.remove('active'));
    panels.forEach(p => p.classList.remove('active'));
    b.classList.add('active');
    document.getElementById(b.dataset.t).classList.add('active');
  }}));
</script>
</body></html>"""


def main():
    print("== デトロイト（MLB StatsAPI）==")
    const = _safe(lambda: mlb_league_constants(), "リーグ定数（wRC+用）")
    const = None if isinstance(const, tuple) else const
    det = {
        "scores": _safe(lambda: mlb_recent_scores(), "スコア"),
        "hitters": _safe(lambda: mlb_top_hitters(MLB_TEAM_ID, MIN_PA, TOP_N, const=const), "1軍打者"),
        "pitchers": _safe(lambda: mlb_top_pitchers(MLB_TEAM_ID, MIN_IP, TOP_N), "1軍投手"),
        "minors": _safe(lambda: mlb_minor_leaders(), "マイナー"),
    }
    print("== 阪神（スクレイピング）==")
    han = {
        "ichigun": _safe(lambda: npb_top_hitters_ichigun(TOP_N), "阪神1軍打者"),
        "farm": _safe(lambda: npb_farm_wrc(TOP_N), "阪神2軍打者"),
    }
    print("== ニュース ==")
    news = collect_news()

    with open("baseball_dashboard.html", "w", encoding="utf-8") as f:
        f.write(build_html(det, han, news))
    print("\n完了: baseball_dashboard.html を生成しました。ブラウザで開いてください。")


if __name__ == "__main__":
    main()
