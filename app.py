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
    replacements = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = text.translate(replacements).lower().strip()
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def get_season_id(match_date: date) -> int:
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

            header_comps = data.get("header", {}).get("competitions", [])
            if header_comps:
                competitors = header_comps[0].get("competitors", [])
                ht_h, ht_a = extract_ht_from_competitors(competitors)
                if ht_h is not None and ht_a is not None:
                    return ht_h, ht_a

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

        my_side = opp_side = home_side = away_side = None
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
        away_name = away_team.get("displayName") or away_team.get("shortDisplayName", "")

        my_ha = my_side.get("homeAway", "")
        venue = "Ev" if my_ha == "home" else ("Dep" if my_ha == "away" else "")

        def extract_score_int(side: dict) -> int | None:
            sc = side.get("score")
            if isinstance(sc, dict):
                return to_int_or_none(sc.get("value") if sc.get("value") is not None else sc.get("displayValue"))
            return to_int_or_none(sc)

        status_completed = comp.get("status", {}).get("type", {}).get("completed", False)
        ft_home = extract_score_int(home_side) if status_completed else None
        ft_away = extract_score_int(away_side) if status_completed else None
        ht_home, ht_away = extract_ht_from_competitors(competitors) if status_completed else (None, None)
        league_slug = ev.get("league", {}).get("slug", "") if isinstance(ev.get("league"), dict) else ""

        parsed.append(
            {
                "event_id": str(ev.get("id", "")),
                "league_slug": league_slug,
                "date": m_date,
                "completed": bool(status_completed),
                "opp_id": opp_id,
                "opp_name": opp_name,
                "venue": venue,
                "home_id": home_id,
                "home_name": home_name,
                "away_id": away_id,
                "away_name": away_name,
                "ft_home": ft_home,
                "ft_away": ft_away,
                "ht_home": ht_home,
                "ht_away": ht_away,
            }
        )
    return parsed


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_season_schedule(club_id: str, league_hint: str, season_id: int) -> list[dict]:
    endpoints = [f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/teams/{club_id}/schedule"]
    if league_hint and league_hint != "all":
        endpoints.append(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_hint}/teams/{club_id}/schedule")

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
    idx = None
    for i, m in enumerate(schedule):
        if m["opp_id"] == str(opp_id) and m["date"] == target_date:
            idx = i
            break
    if idx is None:
        for i, m in enumerate(schedule):
            if m["opp_id"] == str(opp_id) and abs((m["date"] - target_date).days) <= 5:
                idx = i
                break
    if idx is None:
        candidates = [(abs((m["date"] - target_date).days), i) for i, m in enumerate(schedule) if m["opp_id"] == str(opp_id)]
        if candidates:
            candidates.sort()
            idx = candidates[0][1]
    if idx is None:
        for i, m in enumerate(schedule):
            if m["date"] == target_date:
                idx = i
                break

    if idx is None:
        return None, None
    return (schedule[idx - 1] if idx > 0 else None), (schedule[idx + 1] if idx < len(schedule) - 1 else None)


def classify_iy_ms(m: dict) -> tuple[str, str, str]:
    ft_h, ft_a = m.get("ft_home"), m.get("ft_away")
    ht_h, ht_a = m.get("ht_home"), m.get("ht_away")

    ms_str = f"{ft_h}-{ft_a}" if (ft_h is not None and ft_a is not None) else "-:-"
    iy_str = f"{ht_h}-{ht_a}" if (ht_h is not None and ht_a is not None) else "-:-"

    if ft_h is None or ft_a is None or ht_h is None or ht_a is None:
        return iy_str, ms_str, "Bilinmiyor"

    ht_res = "1" if ht_h > ht_a else ("2" if ht_h < ht_a else "X")
    ft_res = "1" if ft_h > ft_a else ("2" if ft_h < ft_a else "X")
    code = f"{ht_res}/{ft_res}"

    if code == "1/2":
        return iy_str, ms_str, "1/2 (İY 1 → MS 2)"
    if code == "2/1":
        return iy_str, ms_str, "2/1 (İY 2 → MS 1)"
    return iy_str, ms_str, f"Normal ({code})"


def format_opp(match_info: dict | None, show_details: bool) -> str:
    if not match_info:
        return "Yok (Sezon başı/sonu)"
    if show_details:
        d_str = match_info["date"].strftime("%d.%m.%Y")
        venue = f" - {match_info['venue']}" if match_info["venue"] else ""
        ft_h, ft_a = match_info.get("ft_home"), match_info.get("ft_away")
        sc_txt = f" [{ft_h}-{ft_a}]" if (ft_h is not None and ft_a is not None) else ""
        return f"{match_info['opp_name']} ({d_str}{venue}{sc_txt})"
    return match_info["opp_name"]


def evaluate_four_opponents(target_opps: dict[str, dict | None], cand_opps: dict[str, dict | None]) -> dict:
    slots = ["a_prev", "a_next", "b_prev", "b_next"]
    target_ids = {s: (target_opps[s]["opp_id"] if target_opps[s] else None) for s in slots}
    cand_ids = {s: (cand_opps[s]["opp_id"] if cand_opps[s] else None) for s in slots}
    cand_names = {s: (cand_opps[s]["opp_name"] if cand_opps[s] else "Yok") for s in slots}

    target_valid_set = {v for v in target_ids.values() if v}
    cand_valid_set = {v for v in cand_ids.values() if v}

    exact_slot_matches = []
    set_matched_slots = set()
    matched_team_names = []

    for s in slots:
        cid = cand_ids[s]
        if not cid:
            continue
        if cid == target_ids[s]:
            exact_slot_matches.append(s)
            set_matched_slots.add(s)
            matched_team_names.append(cand_names[s])
        elif cid in target_valid_set:
            set_matched_slots.add(s)
            matched_team_names.append(cand_names[s])

    unique_matched_teams = list(dict.fromkeys(matched_team_names))
    overlap_count = len(target_valid_set.intersection(cand_valid_set))
    exact_slot_count = len(exact_slot_matches)

    if len(target_valid_set) == 4 and overlap_count == 4 and exact_slot_count == 4:
        summary, level = "4/4 BİREBİR AYNI 4 TAKIM (Aynı Sıra)!", "exact"
    elif len(target_valid_set) == 4 and overlap_count == 4:
        summary, level = "4/4 AYNI 4 TAKIM (Farklı Sıra)!", "exact"
    elif overlap_count > 0:
        summary, level = f"{overlap_count}/4 Benzer ({', '.join(unique_matched_teams)})", "partial"
    else:
        summary, level = "0/4 Farklı", "none"

    return {
        "summary": summary,
        "level": level,
        "overlap_count": overlap_count,
        "exact_slot_count": exact_slot_count,
        "matched_slots": set_matched_slots,
        "matched_teams": unique_matched_teams,
    }def build_colored_excel(
    }def build_colored_excel(
    team_a_name: str,
    team_b_name: str,
    target_date_str: str,
    rows_data: list[dict],
    repeating_summary: list[tuple[str, int, list[str]]],
    identical_pairs: list[str],
) -> bytes:
    wb = Workbook()

    font_title = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    font_subtitle = Font(name="Calibri", size=10, italic=True, color="1B365D")
    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_regular = Font(name="Calibri", size=10, bold=False, color="1F2937")
    font_bold = Font(name="Calibri", size=10, bold=True, color="111827")

    font_target = Font(name="Calibri", size=10, bold=True, color="1B365D")
    font_1_2 = Font(name="Calibri", size=10, bold=True, color="9C2B00")
    font_2_1 = Font(name="Calibri", size=10, bold=True, color="4C1D95")
    font_green = Font(name="Calibri", size=10, bold=True, color="006100")
    font_yellow = Font(name="Calibri", size=10, bold=True, color="7F6000")

    fill_navy = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
    fill_subbanner = PatternFill(start_color="E8EEF5", end_color="E8EEF5", fill_type="solid")
    fill_target = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    fill_zebra = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")
    fill_white = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    fill_1_2 = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
    fill_2_1 = PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid")
    fill_green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    fill_green_strong = PatternFill(start_color="A9D08E", end_color="A9D08E", fill_type="solid")
    fill_yellow = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

    thin_side = Side(border_style="thin", color="D1D5DB")
    med_navy = Side(border_style="medium", color="1B365D")
    border_cell = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    border_target = Border(left=thin_side, right=thin_side, top=med_navy, bottom=med_navy)

    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    ws1 = wb.active
    ws1.title = "1-2_2-1_Analizi"
    ws1.views.sheetView[0].showGridLines = True

    headers_1 = [
        "Kayıt Türü",
        "Tarih",
        "Karşılaşma",
        "İlk Yarı (İY)",
        "Maç Sonu (MS)",
        "Senaryo",
        f"{team_a_name} Önceki Rakip",
        f"{team_a_name} Sonraki Rakip",
        f"{team_b_name} Önceki Rakip",
        f"{team_b_name} Sonraki Rakip",
        "4 Rakip Benzerlik Analizi",
    ]
    total_cols = len(headers_1)

    ws1.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_cols)
    c_title = ws1.cell(
        row=1,
        column=1,
        value=f"⚽ {team_a_name} - {team_b_name} | 1/2 & 2/1 Geri Dönüş ve 4 Çevre Rakip Analizi (Hedef: {target_date_str})",
    )
    c_title.font = font_title
    c_title.fill = fill_navy
    c_title.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws1.row_dimensions[1].height = 30

    ws1.merge_cells(start_row=2, start_column=1, end_row=2, end_column=total_cols)
    c_leg = ws1.cell(
        row=2,
        column=1,
        value="Renk Rehberi:  [Mavi Satır: Hedef Referans Maç]   [Turuncu: 1/2 (İY 1 → MS 2)]   [Mor: 2/1 (İY 2 → MS 1)]   [Yeşil Hücre: Hedef Maçtaki ile Aynı Rakip]",
    )
    c_leg.font = font_subtitle
    c_leg.fill = fill_subbanner
    c_leg.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws1.row_dimensions[2].height = 22

    header_row = 4
    ws1.row_dimensions[header_row].height = 28
    for col_idx, h_text in enumerate(headers_1, start=1):
        cell = ws1.cell(row=header_row, column=col_idx, value=h_text)
        cell.font = font_header
        cell.fill = fill_navy
        cell.alignment = align_center
        cell.border = border_cell

    slot_col_map = {"a_prev": 7, "a_next": 8, "b_prev": 9, "b_next": 10}

    for idx, r in enumerate(rows_data):
        row_num = header_row + 1 + idx
        is_target = r["is_target"]
        scenario = r["scenario"]
        sim = r["similarity"]

        vals = [
            r["kind"],
            r["date"],
            r["match"],
            r["iy"],
            r["ms"],
            scenario,
            r["a_prev_txt"],
            r["a_next_txt"],
            r["b_prev_txt"],
            r["b_next_txt"],
            sim["summary"] if not is_target else "Referans 4 Rakip Seti",
        ]

        base_fill = fill_target if is_target else (fill_zebra if idx % 2 == 1 else fill_white)
        base_font = font_target if is_target else font_regular
        row_border = border_target if is_target else border_cell

        max_lines = 1
        for col_idx, val in enumerate(vals, start=1):
            cell = ws1.cell(row=row_num, column=col_idx, value=val)
            cell.fill = base_fill
            cell.font = base_font
            cell.border = row_border
            cell.alignment = align_center if col_idx in (1, 2, 4, 5, 6) else align_left
            if isinstance(val, str) and len(val) > 28:
                max_lines = max(max_lines, 2)

        if "1/2" in scenario:
            ws1.cell(row=row_num, column=1).fill = fill_1_2
            ws1.cell(row=row_num, column=1).font = font_1_2
            ws1.cell(row=row_num, column=6).fill = fill_1_2
            ws1.cell(row=row_num, column=6).font = font_1_2
        elif "2/1" in scenario:
            ws1.cell(row=row_num, column=1).fill = fill_2_1
            ws1.cell(row=row_num, column=1).font = font_2_1
            ws1.cell(row=row_num, column=6).fill = fill_2_1
            ws1.cell(row=row_num, column=6).font = font_2_1

        if not is_target:
            for slot_key in sim["matched_slots"]:
                c_i = slot_col_map[slot_key]
