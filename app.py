import io
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# --- SAYFA AYARLARI ---
st.set_page_config(page_title="1/2 & 2/1 Rakip Analizi", page_icon="⚽", layout="centered")

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


def to_int_or_none(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


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

    for t in get_turkish_league_teams():
        if norm_q in normalize_tr(t["name"]):
            seen_ids.add(t["id"])
            results.append(t)

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


def extract_ht_from_competitors(competitors: list[dict]) -> tuple[int | None, int | None]:
    """Competitor nesnelerindeki linescores alanından ev ve deplasman ilk yarı skorlarını çıkarır."""
    ht_home, ht_away = None, None
    for c in competitors:
        ha = c.get("homeAway", "")
        ls = c.get("linescores", [])
        if ls and isinstance(ls, list):
            p1 = to_int_or_none(ls[0].get("value") if isinstance(ls[0], dict) else ls[0])
            if ha == "home":
                ht_home = p1
            elif ha == "away":
                ht_away = p1
    return ht_home, ht_away


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_halftime_scores(event_id: str, league_hint: str, home_id: str, away_id: str) -> tuple[int | None, int | None]:
    """ESPN maç özetinden (summary) ilk yarı (İY) ev ve deplasman skorlarını getirir."""
    if not event_id or not event_id.isdigit():
        return None, None

    endpoints = []
    if league_hint and league_hint != "all":
        endpoints.append(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_hint}/summary")
    endpoints.append("https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary")

    for url in endpoints:
        try:
            resp = requests.get(url, params={"event": event_id}, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
            data = resp.json()

            # 1. Yöntem: header -> competitions -> competitors -> linescores
            header_comps = data.get("header", {}).get("competitions", [])
            if header_comps:
                competitors = header_comps[0].get("competitors", [])
                ht_h, ht_a = extract_ht_from_competitors(competitors)
                if ht_h is not None and ht_a is not None:
                    return ht_h, ht_a

                # 2. Yöntem: İlk yarıdaki gol olaylarını (period == 1) say
                details = header_comps[0].get("details", [])
                if details:
                    h_goals, a_goals = 0, 0
                    found_goal_detail = False
                    for d in details:
                        if d.get("scoringPlay"):
                            found_goal_detail = True
                            period = d.get("period", {}).get("number", 0)
                            tid = str(d.get("team", {}).get("id", ""))
                            if period == 1:
                                if tid == str(home_id):
                                    h_goals += 1
                                elif tid == str(away_id):
                                    a_goals += 1
                    if found_goal_detail:
                        return h_goals, a_goals

            # 3. Yöntem: scoringPlays dizisinden 1. devre gollerini say
            scoring_plays = data.get("scoringPlays", [])
            if scoring_plays:
                h_goals, a_goals = 0, 0
                for sp in scoring_plays:
                    period = sp.get("period", {}).get("number", 0)
                    tid = str(sp.get("team", {}).get("id", ""))
                    if period == 1:
                        if tid == str(home_id):
                            h_goals += 1
                        elif tid == str(away_id):
                            a_goals += 1
                return h_goals, a_goals

        except Exception:
            continue

    return None, None


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
        home_side = None
        away_side = None

        for c in competitors:
            c_team = c.get("team", {})
            ha = c.get("homeAway", "")
            if ha == "home":
                home_side = c
            elif ha == "away":
                away_side = c

            if str(c_team.get("id", "")) == str(club_id):
                my_side = c
            else:
                opp_side = c

        if not my_side or not opp_side or not home_side or not away_side:
            continue

        opp_team = opp_side.get("team", {})
        opp_id = str(opp_team.get("id", ""))
        opp_name = opp_team.get("displayName") or opp_team.get("shortDisplayName", "")
        if not opp_id or not opp_name:
            continue

        home_team = home_side.get("team", {})
        away_team = away_side.get("team", {})
        home_id = str(home_team.get("id", ""))
        away_id = str(away_team.get("id", ""))
        home_name = home_team.get("displayName") or home_team.get("shortDisplayName", "")
        away_name = away_team.get("displayName") or away_team.get("
