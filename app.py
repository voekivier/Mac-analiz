import io
import unicodedata
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

# --- SAYFA AYARLARI ---
st.set_page_config(page_title="Fikstür & Rakip Analizi", page_icon="⚽", layout="centered")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

TR_TZ = timezone(timedelta(hours=3))


def normalize_tr(text: str) -> str:
    """Türkçe karakterleri sadeleştirerek arama eşleşmesini kolaylaştırır."""
    replacements = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = text.translate(replacements).lower().strip()
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def get_season_id(match_date: date) -> int:
    """Maç tarihine göre sezon başlangıç yılını (örn. 2026/27 için 2026) döndürür."""
    return match_date.year if match_date.month >= 7 else match_date.year - 1


@st.cache_data(ttl=86400, show_spinner=False)
def get_turkish_league_teams() -> list[dict]:
    """Süper Lig ve TFF 1. Lig takımlarını önbelleğe alır."""
    teams = []
    seen = set()
    for league_code in ("tur.1", "tur.2"):
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/teams"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for sport in data.get("sports", []):
                for league in sport.get("leagues", []):
                    for item in league.get("teams", []):
                        t = item.get("team", {})
                        tid = str(t.get("id", ""))
                        name = t.get("displayName") or t.get("name", "")
                        if tid and name and tid not in seen:
                            seen.add(tid)
                            teams.append({"id": tid, "name": name, "league": league_code})
        except Exception:
            continue
    return teams


@st.cache_data(ttl=3600, show_spinner=False)
def search_clubs(query: str) -> tuple[list[dict], str | None]:
    """ESPN üzerinde dünya genelinde ve Türkiye liglerinde takım arar."""
    q = query.strip()
    if not q:
        return [], "Takım adı boş olamaz."

    norm_q = normalize_tr(q)
    results = []
    seen_ids = set()

    # 1. Önce Türkiye ligleri önbelleğinden hızlı eşleştir
    for t in get_turkish_league_teams():
        if norm_q in normalize_tr(t["name"]):
            seen_ids.add(t["id"])
            results.append(t)

    # 2. ESPN genel futbol araması (Avrupa ve dünya takımları dahil)
    try:
        url = "https://site.web.api.espn.com/apis/common/v3/search"
        resp = requests.get(
            url,
            params={"query": q, "limit": 20, "type": "team"},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("items", []):
                sport = item.get("sport", "")
                uid = item.get("uid", "")
                if sport == "soccer" or "s:600" in uid:
                    tid = str(item.get("id", ""))
                    name = item.get("displayName") or item.get("name", "")
                    league = item.get("league", "all")
                    if tid and name and tid not in seen_ids:
                        seen_ids.add(tid)
                        results.append({"id": tid, "name": name, "league": league})
    except Exception as e:
        if not results:
            return [], f"Bağlantı hatası: {e}"

    if not results:
        return [], "Takım bulunamadı. Yazımı kontrol edip tekrar deneyin."
    return results, None


def parse_espn_events(events: list[dict], club_id: str) -> list[dict]:
    """ESPN etkinlik listesini standart maç formatına dönüştürür."""
    parsed = []
    for ev in events:
        raw_date = ev.get("date", "")
        if not raw_date:
            continue
        try:
            dt_utc = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            m_date = dt_utc.astimezone(TR_TZ).date()
        except ValueError:
            continue

        comps = ev.get("competitions", [])
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        my_side = None
        opp_side = None
        for c in competitors:
            c_team = c.get("team", {})
            if str(c_team.get("id", "")) == str(club_id):
                my_side = c
            else:
                opp_side = c

        if not my_side or not opp_side:
            continue

        opp_team = opp_side.get("team", {})
        opp_id = str(opp_team.get("id", ""))
        opp_name = opp_team.get("displayName") or opp_team.get("shortDisplayName", "")
        if not opp_id or not opp_name:
            continue

        home_away = my_side.get("homeAway", "")
        venue = "Ev" if home_away == "home" else ("Dep" if home_away == "away" else "")

        def extract_score(side: dict) -> str:
            sc = side.get("score")
            if isinstance(sc, dict):
                return str(sc.get("displayValue", ""))
            return str(sc) if sc is not None else ""

        my_sc = extract_score(my_side)
        opp_sc = extract_score(opp_side)
        if my_sc != "" and opp_sc != "":
            score = f"{my_sc}:{opp_sc}" if home_away == "home" else f"{opp_sc}:{my_sc}"
        else:
            score = "-:-"

        home_name = my_side.get("team", {}).get("displayName", "") if home_away == "home" else opp_name
        away_name = opp_name if home_away == "home" else my_side.get("team", {}).get("displayName", "")

        parsed.append(
            {
                "event_id": str(ev.get("id", f"{m_date}_{opp_id}")),
                "date": m_date,
                "opp_id": opp_id,
                "opp_name": opp_name,
                "venue": venue,
                "score": score,
                "home_name": home_name,
                "away_name": away_name,
            }
        )
    return parsed


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_season_schedule(club_id: str, league_hint: str, season_id: int) -> list[dict]:
    """Bir takımın ilgili sezondaki tüm kulvar fikstürünü kronolojik olarak çeker."""
    endpoints = [
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/teams/{club_id}/schedule",
    ]
    if league_hint and league_hint != "all":
        endpoints.append(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_hint}/teams/{club_id}/schedule"
        )

    merged = {}
    for url in endpoints:
        try:
            resp = requests.get(url, params={"season": season_id}, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for m in parse_espn_events(data.get("events", []), club_id):
                key = f"{m['date']}_{m['opp_id']}"
                if key not in merged:
                    merged[key] = m
        except Exception:
            continue

    matches = list(merged.values())
    matches.sort(key=lambda x: x["date"])
    return matches


def find_neighbors(schedule: list[dict], target_date: date, opp_id: str):
    """Fikstür listesinde hedef maçı bulur; bir önceki ve bir sonraki maçı döndürür."""
    idx = None
    # 1. Tam tarih + rakip eşleşmesi
    for i, m in enumerate(schedule):
        if m["opp_id"] == str(opp_id) and m["date"] == target_date:
            idx = i
            break
    # 2. Hafta sonu gün kayması (Cuma-Pazartesi) için ±5 gün toleranslı rakip eşleşmesi
    if idx is None:
        for i, m in enumerate(schedule):
            if m["opp_id"] == str(opp_id) and abs((m["date"] - target_date).days) <= 5:
                idx = i
                break
    # 3. Sezon içinde aynı rakiple oynanan en yakın tarihli maç
    if idx is None:
        candidates = [(abs((m["date"] - target_date).days), i) for i, m in enumerate(schedule) if m["opp_id"] == str(opp_id)]
        if candidates:
            candidates.sort()
            idx = candidates[0][1]
    # 4. Sadece tarih eşleşmesi
    if idx is None:
        for i, m in enumerate(schedule):
            if m["date"] == target_date:
                idx = i
                break

    if idx is None:
        return None, None
    prev_m = schedule[idx - 1] if idx > 0 else None
    next_m = schedule[idx + 1] if idx < len(schedule) - 1 else None
    return prev_m, next_m


def format_opp(match_info: dict | None, show_details: bool) -> str:
    if not match_info:
        return "Yok (Sezon başı/sonu)"
    if show_details:
        d_str = match_info["date"].strftime("%d.%m.%Y")
        venue = f" - {match_info['venue']}" if match_info["venue"] else ""
        return f"{match_info['opp_name']} ({d_str}{venue})"
    return match_info["opp_name"]


# --- ARAYÜZ ---
st.title("⚽ Maç Öncesi & Sonrası Rakip Analizi")
st.caption("Seçilen maç ve geçmiş karşılaşmalar için her iki takımın bir önceki ve bir sonraki rakiplerini listeler.")

col1, col2 = st.columns(2)
with col1:
    team_a_query = st.text_input("1. Takım Adı", value="Galatasaray")
with col2:
    team_b_query = st.text_input("2. Takım Adı", value="Samsunspor")

if st.button("Takımları Getir", type="secondary"):
    with st.spinner("Takımlar aranıyor..."):
        clubs_a, err_a = search_clubs(team_a_query)
        clubs_b, err_b = search_clubs(team_b_query)
        st.session_state["clubs_a"] = clubs_a
        st.session_state["clubs_b"] = clubs_b
        st.session_state["err_a"] = err_a
        st.session_state["err_b"] = err_b

if "clubs_a" in st.session_state and "clubs_b" in st.session_state:
    clubs_a = st.session_state["clubs_a"]
    clubs_b = st.session_state["clubs_b"]
    err_a = st.session_state.get("err_a")
    err_b = st.session_state.get("err_b")

    if not clubs_a or not clubs_b:
        if err_a:
            st.error(f"1. Takım hatası: {err_a}")
        if err_b:
            st.error(f"2. Takım hatası: {err_b}")
    else:
        c1, c2 = st.columns(2)
        with c1:
            sel_a = st.selectbox(
                "1. Takımı Onayla",
                options=clubs_a,
                format_func=lambda x: f"{x['name']} ({x['league'].upper()})",
            )
        with c2:
            sel_b = st.selectbox(
                "2. Takımı Onayla",
                options=clubs_b,
                format_func=lambda x: f"{x['name']} ({x['league'].upper()})",
            )

        c3, c4 = st.columns(2)
        with c3:
            target_date = st.date_input("Analiz Edilecek Maç Tarihi", value=date(2026, 11, 21), format="DD.MM.YYYY")
        with c4:
            h2h_limit = st.slider("İncelenecek Geçmiş Maç Sayısı", min_value=1, max_value=15, value=5)

        show_details = st.checkbox("Rakiplerin yanına tarih ve Ev/Dep bilgisini ekle", value=True)

        if st.button("Analizi Başlat", type="primary"):
            with st.spinner("Fikstürler ve geçmiş karşılaşmalar taranıyor..."):
                target_season = get_season_id(target_date)
                sched_a_curr = fetch_season_schedule(sel_a["id"], sel_a["league"], target_season)
                sched_b_curr = fetch_season_schedule(sel_b["id"], sel_b["league"], target_season)

                prev_a, next_a = find_neighbors(sched_a_curr, target_date, sel_b["id"])
                prev_b, next_b = find_neighbors(sched_b_curr, target_date, sel_a["id"])

                lines = []
                date_short = target_date.strftime("%d.%m.%y")
                lines.append(f"Maç tarihi; {date_short} {sel_a['name']} - {sel_b['name']}")
                lines.append("")
                lines.append(
                    f"{sel_a['name']} bir önceki ve bir sonraki rakip: "
                    f"{format_opp(prev_a, show_details)} ve {format_opp(next_a, show_details)}"
                )
                lines.append("")
                lines.append(
                    f"{sel_b['name']} bir önceki ve bir sonraki rakip: "
                    f"{format_opp(prev_b, show_details)} ve {format_opp(next_b, show_details)}"
                )
                lines.append("")
                lines.append("Önceki karşılaşmalar:")
                lines.append("-" * 45)

                excel_rows = [
                    {
                        "Tür": "Hedef Maç",
                        "Tarih": target_date.strftime("%d.%m.%Y"),
                        "Karşılaşma": f"{sel_a['name']} - {sel_b['name']}",
                        f"{sel_a['name']} Önceki Rakip": format_opp(prev_a, show_details),
                        f"{sel_a['name']} Sonraki Rakip": format_opp(next_a, show_details),
                        f"{sel_b['name']} Önceki Rakip": format_opp(prev_b, show_details),
                        f"{sel_b['name']} Sonraki Rakip": format_opp(next_b, show_details),
                    }
                ]

                # Son 15 sezonu tarayarak geçmiş karşılaşmaları bul
                past_h2h = []
                for s_year in range(target_season, target_season - 16, -1):
                    s_a = fetch_season_schedule(sel_a["id"], sel_a["league"], s_year)
                    for m in reversed(s_a):
                        if m["opp_id"] == str(sel_b["id"]) and m["date"] < target_date:
                            past_h2h.append((s_year, m))
                            if len(past_h2h) >= h2h_limit:
                                break
                    if len(past_h2h) >= h2h_limit:
                        break

                if not past_h2h:
                    lines.append("Son 15 sezonda bu tarihten önce oynanmış karşılaşma bulunamadı.")
                else:
                    for h_season, h in past_h2h:
                        h_date = h["date"]
                        h_date_str = h_date.strftime("%d.%m.%Y")
                        match_title = f"{h_date_str} | {h['home_name']} {h['score']} {h['away_name']}"

                        s_a = fetch_season_schedule(sel_a["id"], sel_a["league"], h_season)
                        s_b = fetch_season_schedule(sel_b["id"], sel_b["league"], h_season)

                        p_a, n_a = find_neighbors(s_a, h_date, sel_b["id"])
                        p_b, n_b = find_neighbors(s_b, h_date, sel_a["id"])

                        lines.append(f"\n• {match_title}")
                        lines.append(
                            f"  {sel_a['name']} bir önceki ve bir sonraki rakip: "
                            f"{format_opp(p_a, show_details)} ve {format_opp(n_a, show_details)}"
                        )
                        lines.append(
                            f"  {sel_b['name']} bir önceki ve bir sonraki rakip: "
                            f"{format_opp(p_b, show_details)} ve {format_opp(n_b, show_details)}"
                        )

                        excel_rows.append(
                            {
                                "Tür": "Geçmiş Karşılaşma",
                                "Tarih": h_date_str,
                                "Karşılaşma": f"{h['home_name']} {h['score']} {h['away_name']}",
                                f"{sel_a['name']} Önceki Rakip": format_opp(p_a, show_details),
                                f"{sel_a['name']} Sonraki Rakip": format_opp(n_a, show_details),
                                f"{sel_b['name']} Önceki Rakip": format_opp(p_b, show_details),
                                f"{sel_b['name']} Sonraki Rakip": format_opp(n_b, show_details),
                            }
                        )

                final_text = "\n".join(lines)

                st.subheader("Analiz Sonucu (Sağ üstten tek tıkla kopyalayabilirsin)")
                st.code(final_text, language=None)

                df_export = pd.DataFrame(excel_rows)
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                    df_export.to_excel(writer, index=False, sheet_name="Rakip Analizi")

                st.download_button(
                    label="📥 Excel Olarak İndir (.xlsx)",
                    data=buffer.getvalue(),
                    file_name=f"{sel_a['name']}_{sel_b['name']}_analiz.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
                
