import io
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
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
        candidates = [
            (abs((m["date"] - target_date).days), i)
            for i, m in enumerate(schedule)
            if m["opp_id"] == str(opp_id)
        ]
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
    prev_m = schedule[idx - 1] if idx > 0 else None
    next_m = schedule[idx + 1] if idx < len(schedule) - 1 else None
    return prev_m, next_m


def classify_iy_ms(m: dict) -> tuple[str, str, str]:
    """Maçın İY skoru, MS skoru ve 1/2 - 2/1 senaryo etiketini döndürür."""
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


def evaluate_four_opponents(
    target_opps: dict[str, dict | None],
    cand_opps: dict[str, dict | None],
) -> dict:
    """
    Hedef maçtaki 4 çevre rakip ile aday maçtaki 4 çevre rakibin benzerliğini inceler.
    Hem pozisyon bazlı (birebir aynı slot) hem de küme bazlı (aynı 4 takım) kontrol yapar.
    """
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
        summary = "4/4 BİREBİR AYNI 4 TAKIM (Aynı Sıra)!"
        level = "exact"
    elif len(target_valid_set) == 4 and overlap_count == 4:
        summary = "4/4 AYNI 4 TAKIM (Farklı Sıra)!"
        level = "exact"
    elif overlap_count > 0:
        summary = f"{overlap_count}/4 Benzer ({', '.join(unique_matched_teams)})"
        level = "partial"
    else:
        summary = "0/4 Farklı"
        level = "none"

    return {
        "summary": summary,
        "level": level,
        "overlap_count": overlap_count,
        "exact_slot_count": exact_slot_count,
        "matched_slots": set_matched_slots,
        "matched_teams": unique_matched_teams,
    }


def build_colored_excel(
    team_a_name: str,
    team_b_name: str,
    target_date_str: str,
    rows_data: list[dict],
    repeating_summary: list[tuple[str, int, list[str]]],
    identical_pairs: list[str],
) -> bytes:
    """Renklendirilmiş, okuması kolay 2 sayfalı Excel çalışma kitabı oluşturur."""
    wb = pd.ExcelWriter(io.BytesIO(), engine="openpyxl").book
    # Boş varsayılan sayfayı temizle
    wb.remove(wb.active)

    # --- STİL TANIMLARI ---
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

    fill_1_2 = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")  # Turuncu/Şeftali
    fill_2_1 = PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid")  # Açık Mor
    fill_green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")  # Yeşil (Eşleşen Rakip)
    fill_green_strong = PatternFill(start_color="A9D08E", end_color="A9D08E", fill_type="solid")  # Koyu Yeşil (4/4)
    fill_yellow = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")  # Sarı (Kısmi Benzer)

    thin_side = Side(border_style="thin", color="D1D5DB")
    med_navy = Side(border_style="medium", color="1B365D")
    border_cell = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    border_target = Border(left=thin_side, right=thin_side, top=med_navy, bottom=med_navy)

    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # ==========================================
    # SAYFA 1: 1/2 & 2/1 GERİ DÖNÜŞ ANALİZİ
    # ==========================================
    ws1 = wb.create_sheet(title="1-2_2-1_Analizi")
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

    # Başlık Satırı
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

    # Renk Açıklama Satırı (Legend)
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

    # Tablo Başlıkları (Satır 4)
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

        # Senaryo Hücresi Renklendirme (1/2 Turuncu, 2/1 Mor)
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

        # Eşleşen Çevre Rakip Hücrelerini Yeşil Vurgula
        if not is_target:
            for slot_key in sim["matched_slots"]:
                c_i = slot_col_map[slot_key]
                ws1.cell(row=row_num, column=c_i).fill = fill_green
                ws1.cell(row=row_num, column=c_i).font = font_green

            sim_cell = ws1.cell(row=row_num, column=11)
            if sim["level"] == "exact":
                sim_cell.fill = fill_green_strong
                sim_cell.font = font_green
            elif sim["level"] == "partial":
                sim_cell.fill = fill_yellow
                sim_cell.font = font_yellow

        ws1.row_dimensions[row_num].height = 26 if max_lines == 1 else 36

    ws1.freeze_panes = "A5"

    # Sütun Genişliklerini Otomatik Ayarla
    col_widths_1 = [18, 13, 28, 14, 14, 21, 30, 30, 30, 30, 34]
    for col_idx, width in enumerate(col_widths_1, start=1):
        ws1.column_dimensions[get_column_letter(col_idx)].width = width

    # ==========================================
    # SAYFA 2: 4 RAKİP KARŞILAŞTIRMA & TEKRAR ÖZETİ
    # ==========================================
    ws2 = wb.create_sheet(title="4_Rakip_Karsilastirma")
    ws2.views.sheetView[0].showGridLines = True

    ws2.merge_cells("A1:E1")
    t2 = ws2.cell(row=1, column=1, value="📊 4 Çevre Rakip Benzerlik ve Tekrar Analizi Özeti")
    t2.font = font_title
    t2.fill = fill_navy
    t2.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws2.row_dimensions[1].height = 30

    # Bölüm A: Tekrar Eden Rakipler Tablosu
    ws2.cell(row=3, column=1, value="1. Geri Dönüş (1/2 & 2/1) Maçlarında Tekrar Eden Çevre Rakipler").font = font_bold
    headers_2a = ["Rakip Takım", "Görünme Sayısı", "Bulunduğu Maç ve Pozisyonlar"]
    ws2.row_dimensions[4].height = 25
    for c_i, h_t in enumerate(headers_2a, start=1):
        c = ws2.cell(row=4, column=c_i, value=h_t)
        c.font = font_header
        c.fill = fill_navy
        c.alignment = align_center if c_i == 2 else align_left
        c.border = border_cell

    curr_r = 5
    if not repeating_summary:
        ws2.cell(row=curr_r, column=1, value="Tekrar eden ortak çevre rakip bulunamadı.").font = font_regular
        curr_r += 2
    else:
        for team_n, cnt, roles in repeating_summary:
            c1 = ws2.cell(row=curr_r, column=1, value=team_n)
            c2 = ws2.cell(row=curr_r, column=2, value=f"{cnt} kez")
            c3 = ws2.cell(row=curr_r, column=3, value=" | ".join(roles))
            for cx in (c1, c2, c3):
                cx.font = font_green if cnt >= 2 else font_regular
                cx.fill = fill_green if cnt >= 2 else fill_white
                cx.border = border_cell
                cx.alignment = align_center if cx == c2 else align_left
            ws2.row_dimensions[curr_r].height = 24
            curr_r += 1
        curr_r += 1

    # Bölüm B: Birebir Aynı 4 Takım Seti Kontrolü
    ws2.cell(row=curr_r, column=1, value="2. Aynı 4 Rakip Kümesine Sahip Eşleşmeler").font = font_bold
    curr_r += 1
    if not identical_pairs:
        c_no = ws2.cell(
            row=curr_r,
            column=1,
            value="İncelenen maçlar arasında 4 çevre rakibin tamamının birebir aynı olduğu bir eşleşme bulunmadı.",
        )
        c_no.font = font_regular
    else:
        for pair_txt in identical_pairs:
            ws2.merge_cells(start_row=curr_r, start_column=1, end_row=curr_r, end_column=3)
            c_yes = ws2.cell(row=curr_r, column=1, value=pair_txt)
            c_yes.font = font_green
            c_yes.fill = fill_green_strong
            c_yes.alignment = align_left
            ws2.row_dimensions[curr_r].height = 26
            curr_r += 1

    ws2.column_dimensions["A"].width = 30
    ws2.column_dimensions["B"].width = 18
    ws2.column_dimensions["C"].width = 65

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --- ARAYÜZ ---
st.title("⚽ 1/2 & 2/1 Geri Dönüş ve Rakip Analizi")
st.caption(
    "Seçilen iki takım arasındaki İY/MS geri dönüş (1/2 ve 2/1) maçlarını bulur, "
    "bir önceki/sonraki 4 rakibin benzerliğini inceler ve renklendirilmiş Excel üretir."
)

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
            seasons_back = st.slider("Taranacak Geçmiş Sezon Sayısı", min_value=5, max_value=25, value=15)

        opt1, opt2 = st.columns(2)
        with opt1:
            only_comebacks = st.checkbox("Sadece 1/2 ve 2/1 geri dönüş maçlarını çıkar", value=True)
        with opt2:
            show_details = st.checkbox("Rakiplerin yanına tarih, Ev/Dep ve skor ekle", value=True)

        if st.button("Analizi Başlat", type="primary"):
            with st.spinner("Sezon fikstürleri ve ilk yarı (İY) skorları taranıyor..."):
                target_season = get_season_id(target_date)
                season_years = list(range(target_season, target_season - seasons_back, -1))

                # Tüm sezonların fikstürlerini paralel çek
                sched_a_map = {}
                sched_b_map = {}

                def load_pair(sy):
                    sa = fetch_season_schedule(sel_a["id"], sel_a["league"], sy)
                    sb = fetch_season_schedule(sel_b["id"], sel_b["league"], sy)
                    return sy, sa, sb

                with ThreadPoolExecutor(max_workers=6) as pool:
                    for sy, sa, sb in pool.map(load_pair, season_years):
                        sched_a_map[sy] = sa
                        sched_b_map[sy] = sb

                # Hedef maçın çevre rakipleri
                sched_a_curr = sched_a_map.get(target_season, [])
                sched_b_curr = sched_b_map.get(target_season, [])
                prev_a, next_a = find_neighbors(sched_a_curr, target_date, sel_b["id"])
                prev_b, next_b = find_neighbors(sched_b_curr, target_date, sel_a["id"])

                target_opps = {
                    "a_prev": prev_a,
                    "a_next": next_a,
                    "b_prev": prev_b,
                    "b_next": next_b,
                }

                # Hedef maç eğer oynanmışsa onun da İY/MS bilgisini kontrol et
                target_match_obj = None
                for m in sched_a_curr:
                    if m["opp_id"] == str(sel_b["id"]) and abs((m["date"] - target_date).days) <= 5:
                        target_match_obj = dict(m)
                        break

                if target_match_obj and target_match_obj.get("completed"):
                    if target_match_obj["ht_home"] is None:
                        ht_h, ht_a = fetch_halftime_scores(
                            target_match_obj["event_id"],
                            target_match_obj.get("league_slug") or sel_a["league"],
                            target_match_obj["home_id"],
                            target_match_obj["away_id"],
                        )
                        target_match_obj["ht_home"], target_match_obj["ht_away"] = ht_h, ht_a
                    t_iy, t_ms, t_scen = classify_iy_ms(target_match_obj)
                else:
                    t_iy, t_ms, t_scen = "-:-", "-:-", "Hedef Maç (Referans)"

                # Geçmiş H2H maçlarını topla
                raw_h2h = []
                seen_h2h_dates = set()
                for sy in season_years:
                    for m in reversed(sched_a_map.get(sy, [])):
                        if m["opp_id"] == str(sel_b["id"]) and m["date"] < target_date and m["completed"]:
                            d_key = m["date"].isoformat()
                            if d_key not in seen_h2h_dates:
                                seen_h2h_dates.add(d_key)
                                raw_h2h.append((sy, dict(m)))

                # H2H maçları için ilk yarı (İY) skorlarını paralel çek
                def enrich_ht(item):
                    sy, m = item
                    if m["ht_home"] is None or m["ht_away"] is None:
                        ht_h, ht_a = fetch_halftime_scores(
                            m["event_id"],
                            m.get("league_slug") or sel_a["league"],
                            m["home_id"],
                            m["away_id"],
                        )
                        m["ht_home"], m["ht_away"] = ht_h, ht_a
                    iy_s, ms_s, scen_s = classify_iy_ms(m)
                    return sy, m, iy_s, ms_s, scen_s

                enriched_h2h = []
                if raw_h2h:
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        enriched_h2h = list(pool.map(enrich_ht, raw_h2h))

                # Filtreleme (Sadece 1/2 ve 2/1 veya tümü)
                filtered_h2h = []
                for sy, m, iy_s, ms_s, scen_s in enriched_h2h:
                    is_comeback = ("1/2" in scen_s) or ("2/1" in scen_s)
                    if only_comebacks and not is_comeback:
                        continue
                    filtered_h2h.append((sy, m, iy_s, ms_s, scen_s))

                # Excel ve Metin Satırlarını Hazırla
                rows_data = [
                    {
                        "is_target": True,
                        "kind": "Hedef Maç",
                        "date": target_date.strftime("%d.%m.%Y"),
                        "match": f"{sel_a['name']} - {sel_b['name']}",
                        "iy": t_iy,
                        "ms": t_ms,
                        "scenario": t_scen,
                        "a_prev_txt": format_opp(prev_a, show_details),
                        "a_next_txt": format_opp(next_a, show_details),
                        "b_prev_txt": format_opp(prev_b, show_details),
                        "b_next_txt": format_opp(next_b, show_details),
                        "opps": target_opps,
                        "similarity": {
                            "summary": "Referans",
                            "level": "target",
                            "overlap_count": 4,
                            "exact_slot_count": 4,
                            "matched_slots": set(),
                            "matched_teams": [],
                        },
                    }
                ]

                lines = []
                date_short = target_date.strftime("%d.%m.%y")
                lines.append(f"Hedef Maç: {date_short} {sel_a['name']} - {sel_b['name']} (İY: {t_iy} | MS: {t_ms})")
                lines.append(
                    f"  • {sel_a['name']} Önceki & Sonraki: "
                    f"{format_opp(prev_a, show_details)}  |  {format_opp(next_a, show_details)}"
                )
                lines.append(
                    f"  • {sel_b['name']} Önceki & Sonraki: "
                    f"{format_opp(prev_b, show_details)}  |  {format_opp(next_b, show_details)}"
                )
                lines.append("")
                lines.append(
                    f"Bulunan {'1/2 ve 2/1 Geri Dönüş' if only_comebacks else 'Geçmiş'} Karşılaşmaları "
                    f"(Taranan: {len(raw_h2h)} maç, Listelenen: {len(filtered_h2h)} maç):"
                )
                lines.append("=" * 55)

                # Tekrar ve 4'lü Küme İncelemesi için Veri Yapıları
                opp_counter = Counter()
                opp_roles_map: dict[str, list[str]] = {}
                four_sets_by_match = []

                # Hedef maçın 4'lü setini de kaydet
                t_set = frozenset(v["opp_id"] for v in target_opps.values() if v)
                if len(t_set) == 4:
                    four_sets_by_match.append((f"Hedef Maç ({target_date.strftime('%d.%m.%Y')})", t_set, target_opps))

                if not filtered_h2h:
                    lines.append("Seçilen sezon aralığında 1/2 veya 2/1 geri dönüş maçı bulunamadı.")
                else:
                    for h_season, h, iy_s, ms_s, scen_s in filtered_h2h:
                        h_date = h["date"]
                        h_date_str = h_date.strftime("%d.%m.%Y")
                        match_title = f"{h['home_name']} - {h['away_name']}"

                        s_a = sched_a_map.get(h_season, [])
                        s_b = sched_b_map.get(h_season, [])

                        p_a, n_a = find_neighbors(s_a, h_date, sel_b["id"])
                        p_b, n_b = find_neighbors(s_b, h_date, sel_a["id"])

                        cand_opps = {"a_prev": p_a, "a_next": n_a, "b_prev": p_b, "b_next": n_b}
                        sim = evaluate_four_opponents(target_opps, cand_opps)

                        # Tekrar analizi için sayaçları güncelle
                        role_labels = [
                            (p_a, f"{h_date_str} ({sel_a['name']} Önceki)"),
                            (n_a, f"{h_date_str} ({sel_a['name']} Sonraki)"),
                            (p_b, f"{h_date_str} ({sel_b['name']} Önceki)"),
                            (n_b, f"{h_date_str} ({sel_b['name']} Sonraki)"),
                        ]
                        for m_obj, r_lbl in role_labels:
                            if m_obj:
                                tname = m_obj["opp_name"]
                                opp_counter[tname] += 1
                                opp_roles_map.setdefault(tname, []).append(r_lbl)

                        c_set = frozenset(v["opp_id"] for v in cand_opps.values() if v)
                        if len(c_set) == 4:
                            four_sets_by_match.append((f"{h_date_str} {match_title} [{scen_s}]", c_set, cand_opps))

                        rows_data.append(
                            {
                                "is_target": False,
                                "kind": "Geri Dönüş (1/2)" if "1/2" in scen_s else ("Geri Dönüş (2/1)" if "2/1" in scen_s else "Geçmiş Maç"),
                                "date": h_date_str,
                                "match": match_title,
                                "iy": iy_s,
                                "ms": ms_s,
                                "scenario": scen_s,
                                "a_prev_txt": format_opp(p_a, show_details),
                                "a_next_txt": format_opp(n_a, show_details),
                                "b_prev_txt": format_opp(p_b, show_details),
                                "b_next_txt": format_opp(n_b, show_details),
                                "opps": cand_opps,
                                "similarity": sim,
                            }
                        )

                        lines.append(f"\n• {h_date_str} | {match_title} | İY: {iy_s} / MS: {ms_s} -> [{scen_s}]")
                        lines.append(
                            f"  {sel_a['name']} Önceki & Sonraki: "
                            f"{format_opp(p_a, show_details)}  |  {format_opp(n_a, show_details)}"
                        )
                        lines.append(
                            f"  {sel_b['name']} Önceki & Sonraki: "
                            f"{format_opp(p_b, show_details)}  |  {format_opp(n_b, show_details)}"
                        )
                        lines.append(f"  4 Rakip Benzerliği (Hedef Maç ile): {sim['summary']}")

                # Birebir Aynı 4 Takım Kümesine Sahip Eşleşmeleri Bul
                identical_pairs = []
                for i in range(len(four_sets_by_match)):
                    for j in range(i + 1, len(four_sets_by_match)):
                        lbl_i, set_i, opps_i = four_sets_by_match[i]
                        lbl_j, set_j, _ = four_sets_by_match[j]
                        if set_i == set_j:
                            team_list = ", ".join(v["opp_name"] for v in opps_i.values() if v)
                            identical_pairs.append(f"AYNI 4 TAKIM: {lbl_i}  <==>  {lbl_j}  ({team_list})")

                repeating_summary = [
                    (tname, cnt, opp_roles_map[tname])
                    for tname, cnt in opp_counter.most_common()
                    if cnt >= 2
                ]

                lines.append("\n" + "=" * 55)
                lines.append("4 ÇEVRE RAKİP BENZERLİK & TEKRAR ÖZETİ:")
                if identical_pairs:
                    for ip in identical_pairs:
                        lines.append(f"  ★ {ip}")
                else:
                    lines.append("  • İncelenen maçlar arasında 4 çevre rakibin tamamının birebir aynı olduğu eşleşme yok.")

                if repeating_summary:
                    lines.append("  • Geri dönüş maçlarında birden fazla kez görünen rakipler:")
                    for tname, cnt, roles in repeating_summary:
                        lines.append(f"    - {tname} ({cnt} kez): {', '.join(roles)}")

                # --- EKRAN ÇIKTILARI ---
                m1, m2, m3 = st.columns(3)
                m1.metric("Taranan Geçmiş Maç", len(raw_h2h))
                m2.metric("Bulunan 1/2 - 2/1", len(filtered_h2h))
                m3.metric("4/4 Aynı Rakip Seti", len(identical_pairs))

                if identical_pairs:
                    for ip in identical_pairs:
                        st.success(f"🎯 {ip}")

                if repeating_summary:
                    with st.expander("🔁 Geri Dönüş Maçlarında Tekrar Eden Çevre Rakipler", expanded=True):
                        for tname, cnt, roles in repeating_summary:
                            st.write(f"**{tname}** ({cnt} kez): {' • '.join(roles)}")

                st.subheader("Analiz Sonucu (Sağ üstten tek tıkla kopyalayabilirsin)")
                st.code("\n".join(lines), language=None)

                excel_bytes = build_colored_excel(
                    team_a_name=sel_a["name"],
                    team_b_name=sel_b["name"],
                    target_date_str=target_date.strftime("%d.%m.%Y"),
                    rows_data=rows_data,
                    repeating_summary=repeating_summary,
                    identical_pairs=identical_pairs,
                )

                st.download_button(
                    label="📥 Renklendirilmiş Excel İndir (.xlsx)",
                    data=excel_bytes,
                    file_name=f"{sel_a['name']}_{sel_b['name']}_1-2_2-1_analiz.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
