import re
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="İY/MS 1/2 & 2/1 Analiz", page_icon="⚽", layout="wide")
st.markdown(
    """<style>
    .block-container{padding-top:1.2rem;max-width:1240px}
    .kpi{background:linear-gradient(135deg,#111827,#1e293b);border:1px solid #334155;
         border-radius:12px;padding:14px 16px;color:#f8fafc}
    .kpi-t{font-size:.76rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em}
    .kpi-v{font-size:1.55rem;font-weight:700;margin-top:2px}
    .kpi-s{font-size:.78rem;color:#38bdf8;margin-top:2px}
    </style>""",
    unsafe_allow_html=True,
)

API = "https://site.api.espn.com/apis/site/v2/sports/soccer"
HDR = {"User-Agent": "Mozilla/5.0"}
LEAGUES = {
    "🇹🇷 Süper Lig": "tur.1",
    "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League": "eng.1",
    "🇪🇸 La Liga": "esp.1",
    "🇮🇹 Serie A": "ita.1",
    "🇩🇪 Bundesliga": "ger.1",
    "🇫🇷 Ligue 1": "fra.1",
    "🇳🇱 Eredivisie": "ned.1",
    "🇵🇹 Primeira Liga": "por.1",
    "🇧🇪 Pro League": "bel.1",
    "🏆 Şampiyonlar Ligi": "uefa.champions",
    "🟠 Avrupa Ligi": "uefa.europa",
}
RES = lambda h, a: "1" if h > a else ("2" if a > h else "X")


@st.cache_data(ttl=86400, show_spinner=False)
def search_teams(q: str):
    r = requests.get(
        "https://site.web.api.espn.com/apis/common/v3/search",
        params={"query": q, "limit": 15, "type": "team", "sport": "soccer"},
        headers=HDR,
        timeout=10,
    ).json()
    out, seen = [], set()
    for it in r.get("items", []):
        tid = str(it.get("id", ""))
        if tid and tid not in seen:
            seen.add(tid)
            out.append({
                "id": tid,
                "name": it.get("displayName") or it.get("name", ""),
                "league": it.get("league", "all"),
            })
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def league_teams(code: str):
    r = requests.get(f"{API}/{code}/teams", headers=HDR, timeout=10).json()
    teams = r.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    return [
        {"id": str(t["team"]["id"]), "name": t["team"]["displayName"], "league": code}
        for t in teams
    ]


def parse_ev(events: list, target_id: str):
    rows = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        st_type = (comp.get("status") or ev.get("status") or {}).get("type", {})
        if not st_type.get("completed", False) and st_type.get("state") != "post":
            continue

        cs = comp.get("competitors") or []
        if len(cs) < 2:
            continue
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        hid, aid = str(h["team"]["id"]), str(a["team"]["id"])
        if target_id not in (hid, aid):
            continue

        sc = lambda x: x["score"].get("value") if isinstance(x.get("score"), dict) else x.get("score")
        if sc(h) is None or sc(a) is None:
            continue
        try:
            fh, fa = int(float(sc(h))), int(float(sc(a)))
        except (ValueError, TypeError):
            continue

        oid = aid if hid == target_id else hid
        oname = a["team"]["displayName"] if hid == target_id else h["team"]["displayName"]
        lg_name = (ev.get("league") or {}).get("abbreviation") or (ev.get("season") or {}).get("slug", "")
        rows.append({
            "eid": str(ev["id"]),
            "date": ev.get("date", "")[:10],
            "comp": lg_name,
            "hid": hid,
            "aid": aid,
            "home": h["team"]["displayName"],
            "away": a["team"]["displayName"],
            "oid": oid,
            "opp": oname,
            "is_home": hid == target_id,
            "fh": fh,
            "fa": fa,
        })
    return rows


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sched(cid: str, lg: str, yr: int):
    merged = {}
    urls = [f"{API}/all/teams/{cid}/schedule"]
    if lg and lg != "all":
        urls.append(f"{API}/{lg}/teams/{cid}/schedule")

    for u in urls:
        try:
            data = requests.get(u, params={"season": yr}, headers=HDR, timeout=12).json()
            for m in parse_ev(data.get("events", []), cid):
                merged[m["eid"]] = m
        except Exception:
            pass
    return sorted(merged.values(), key=lambda x: x["date"])


def _is_goal_event(ev: dict) -> bool:
    if ev.get("scoringPlay") is True:
        return True
    txt = ((ev.get("type") or {}).get("text") or ev.get("text") or "").lower()
    if any(bad in txt for bad in ("missed", "saved", "disallowed", "no goal", "offside")):
        return False
    return ("goal" in txt) or ("penalty - scored" in txt)


def _extract_minute(clk_str: str) -> int:
    m = re.match(r"(\d+)", str(clk_str or "").strip())
    return int(m.group(1)) if m else 999


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_ht(eid: str, hid: str, aid: str, ft_h: int, ft_a: int):
    """Dönüş: (ht_h, ht_a, reg_h, reg_a, ht_known)"""
    try:
        r = requests.get(f"{API}/all/summary", params={"event": eid}, headers=HDR, timeout=12).json()
        comp = (r.get("header") or {}).get("competitions", [{}])[0]

        # 1) Linescores kontrolü (varsa en doğrudan kaynak)
        cs = comp.get("competitors") or []
        if len(cs) >= 2:
            hc = next((c for c in cs if str((c.get("team") or {}).get("id")) == hid), cs[0])
            ac = next((c for c in cs if str((c.get("team") or {}).get("id")) == aid), cs[1])
            hl, al = hc.get("linescores") or [], ac.get("linescores") or []
            if len(hl) >= 2 and len(al) >= 2 and "value" in hl[0] and "value" in al[0]:
                h1, a1 = int(float(hl[0]["value"])), int(float(al[0]["value"]))
                h2, a2 = int(float(hl[1]["value"])), int(float(al[1]["value"]))
                return h1, a1, h1 + h2, a1 + a2, True

        # 2) ESPN Soccer gol zaman çizelgesi: keyEvents -> header.competitions[0].details -> details
        raw_events = (
            r.get("keyEvents")
            or comp.get("details")
            or r.get("details")
            or r.get("scoringPlays")
            or []
        )

        goals = []
        for d in raw_events:
            if not _is_goal_event(d):
                continue
            per = int((d.get("period") or {}).get("number", 0) or 0)
            if per == 5:  # Seri penaltı atışlarını hariç tut
                continue
            clk = (d.get("clock") or {}).get("displayValue", "")
            minute = _extract_minute(clk)
            is_1h = (per == 1) or (per == 0 and minute <= 45)
            is_reg = (per in (1, 2)) or (per == 0 and minute <= 90)

            tid = str((d.get("team") or {}).get("id") or d.get("teamId") or "")
            is_og = bool(d.get("ownGoal")) or ("own goal" in ((d.get("type") or {}).get("text") or "").lower())
            goals.append({"tid": tid, "is_1h": is_1h, "is_reg": is_reg, "is_og": is_og})

        if not goals:
            if ft_h == 0 and ft_a == 0:
                return 0, 0, 0, 0, True
            return None, None, ft_h, ft_a, False

        def tally(flip_og: bool):
            h1 = a1 = hr = ar = ht_all = at_all = 0
            for g in goals:
                t = g["tid"]
                if flip_og and g["is_og"] and t in (hid, aid):
                    t = aid if t == hid else hid
                if t == hid:
                    ht_all += 1
                    if g["is_reg"]:
                        hr += 1
                    if g["is_1h"]:
                        h1 += 1
                elif t == aid:
                    at_all += 1
                    if g["is_reg"]:
                        ar += 1
                    if g["is_1h"]:
                        a1 += 1
            return h1, a1, hr, ar, ht_all, at_all

        h1, a1, hr, ar, tot_h, tot_a = tally(flip_og=False)

        # Kendi kalesine gol ters takıma yazılmışsa otomatik düzelt
        if (tot_h, tot_a) != (ft_h, ft_a) and any(g["is_og"] for g in goals):
            fh1, fa1, fhr, far, ftot_h, ftot_a = tally(flip_og=True)
            if (ftot_h, ftot_a) == (ft_h, ft_a):
                return fh1, fa1, fhr, far, True

        return h1, a1, hr, ar, True
    except Exception:
        if ft_h == 0 and ft_a == 0:
            return 0, 0, 0, 0, True
        return None, None, ft_h, ft_a, False


def build_df(matches: list, cid: str):
    if not matches:
        return pd.DataFrame()
    with ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(lambda m: fetch_ht(m["eid"], m["hid"], m["aid"], m["fh"], m["fa"]), matches))

    out = []
    for m, (h1, a1, rh, ra, known) in zip(matches, res):
        if known and h1 is not None and a1 is not None:
            ht_r = RES(h1, a1)
            ft_r = RES(rh, ra)
            iyms = f"{ht_r}/{ft_r}"
            iy_txt = f"{h1}-{a1}"
            is_rev = iyms in ("1/2", "2/1")
            if iyms == "2/1":
                rev_team = m["home"]
            elif iyms == "1/2":
                rev_team = m["away"]
            else:
                rev_team = "-"
        else:
            iyms = "Veri Yok"
            iy_txt = "?"
            is_rev = False
            rev_team = "-"

        ms_txt = f"{rh}-{ra}" if (rh, ra) == (m["fh"], m["fa"]) else f"{rh}-{ra} (Uzt: {m['fh']}-{m['fa']})"
        out.append({
            "Tarih": m["date"],
            "Turnuva": m["comp"],
            "Ev Sahibi": m["home"],
            "Deplasman": m["away"],
            "İY": iy_txt,
            "MS (90')": ms_txt,
            "İY/MS": iyms,
            "1/2-2/1": is_rev,
            "Geri Dönen": rev_team,
            "Saha": "Ev" if m["is_home"] else "Dep",
            "oid": m["oid"],
            "opp": m["opp"],
        })
    return pd.DataFrame(out)


def pick_team(label: str, key: str):
    c1, c2 = st.columns([1, 2])
    mode = c1.radio(label, ["Ligden Seç", "İsimle Ara"], key=f"m_{key}", horizontal=True)
    if mode == "Ligden Seç":
        lg = c2.selectbox("Lig", list(LEAGUES.keys()), key=f"l_{key}")
        ts = league_teams(LEAGUES[lg])
        return c2.selectbox("Takım", ts, format_func=lambda x: x["name"], key=f"t_{key}") if ts else None
    q = c2.text_input("Takım adı (örn. Galatasaray, Barcelona, Arsenal)", key=f"q_{key}")
    if len(q.strip()) < 2:
        return None
    ts = search_teams(q.strip())
    return c2.selectbox("Eşleşen Takımlar", ts, format_func=lambda x: f"{x['name']} ({x['league']})", key=f"s_{key}") if ts else None


st.title("⚽ İY/MS (1/2 & 2/1) Geçmiş Maç Analizi")
with st.sidebar:
    st.header("Filtreler")
    yrs = st.slider("Sezon Aralığı", 2010, 2026, (2019, 2026))
    analysis = st.radio("Analiz Türü", ["İki Takım Arası (H2H)", "Tek Takım Sezon Geçmişi"])
    if st.button("🔄 Önbelleği Temizle"):
        st.cache_data.clear()
        st.rerun()

t1 = pick_team("1. Takım", "t1")
t2 = pick_team("2. Takım (Rakip)", "t2") if analysis == "İki Takım Arası (H2H)" else None

if st.button("🔍 Analizi Başlat", type="primary", use_container_width=True):
    if not t1 or (analysis == "İki Takım Arası (H2H)" and not t2):
        st.warning("Lütfen takım seçimini tamamlayın.")
        st.stop()
    if t2 and t1["id"] == t2["id"]:
        st.warning("Lütfen iki farklı takım seçin.")
        st.stop()

    with st.spinner("Maçlar ve ilk yarı gol dakikaları çekiliyor..."):
        raw = [m for y in range(yrs[0], yrs[1] + 1) for m in fetch_sched(t1["id"], t1["league"], y)]
        dedup = list({m["eid"]: m for m in raw}.values())
        if t2:
            dedup = [m for m in dedup if m["oid"] == t2["id"]]
        df = build_df(sorted(dedup, key=lambda x: x["date"], reverse=True), t1["id"])

    if df.empty:
        st.info("Seçilen sezon aralığında tamamlanmış maç bulunamadı.")
        st.stop()

    valid = df[df["İY/MS"] != "Veri Yok"]
    tot = len(valid)
    c_12 = int((valid["İY/MS"] == "1/2").sum())
    c_21 = int((valid["İY/MS"] == "2/1").sum())
    rev = c_12 + c_21
    pct = (rev / tot * 100) if tot else 0.0

    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(f'<div class="kpi"><div class="kpi-t">İY Verisi Olan Maç</div><div class="kpi-v">{tot} / {len(df)}</div><div class="kpi-s">{yrs[0]}–{yrs[1]}</div></div>', unsafe_allow_html=True)
    k2.markdown(f'<div class="kpi"><div class="kpi-t">Toplam 1/2 + 2/1</div><div class="kpi-v">{rev}</div><div class="kpi-s">Oran: %{pct:.1f}</div></div>', unsafe_allow_html=True)
    k3.markdown(f'<div class="kpi"><div class="kpi-t">1/2 (Ev İY → Dep MS)</div><div class="kpi-v">{c_12}</div><div class="kpi-s">Deplasman geri dönüşü</div></div>', unsafe_allow_html=True)
    k4.markdown(f'<div class="kpi"><div class="kpi-t">2/1 (Dep İY → Ev MS)</div><div class="kpi-v">{c_21}</div><div class="kpi-s">Ev sahibi geri dönüşü</div></div>', unsafe_allow_html=True)

    st.subheader("🔥 1/2 ve 2/1 Biten Maçlar")
    rev_df = df[df["1/2-2/1"]].drop(columns=["1/2-2/1", "oid", "opp"])
    if rev_df.empty:
        st.info("Bu filtrede 1/2 veya 2/1 biten maç yok.")
    else:
        st.dataframe(rev_df, use_container_width=True, hide_index=True)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("📊 İY/MS Dağılımı")
        dist = df["İY/MS"].value_counts().reset_index()
        dist.columns = ["İY/MS", "Maç"]
        dist["Oran (%)"] = (dist["Maç"] / len(df) * 100).round(1)
        st.dataframe(dist, use_container_width=True, hide_index=True)

    with col_b:
        if not t2 and not valid.empty:
            st.subheader("🎯 En Çok 1/2 - 2/1 Oynanan Rakipler")
            opp = valid.groupby("opp").agg(Maç=("İY/MS", "count"), Geri_Dönüş=("1/2-2/1", "sum")).reset_index()
            opp = opp[opp["Geri_Dönüş"] > 0].sort_values(["Geri_Dönüş", "Maç"], ascending=False)
            opp.columns = ["Rakip", "Maç", "1/2 + 2/1"]
            st.dataframe(opp, use_container_width=True, hide_index=True)

    with st.expander("📋 Tüm Maç Listesi"):
        st.dataframe(df.drop(columns=["1/2-2/1", "oid", "opp"]), use_container_width=True, hide_index=True)
