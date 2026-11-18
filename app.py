import io
import re
import time
from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --- SAYFA AYARLARI ---
st.set_page_config(page_title="Fikstür & Rakip Analizi", page_icon="⚽", layout="centered")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.transfermarkt.com.tr/",
}

# --- YARDIMCI FONKSİYONLAR ---


def parse_tm_date(text: str) -> date | None:
    """Transfermarkt tarih formatlarını (21.11.2026 veya 21.11.26) date objesine çevirir."""
    match = re.search(r"(\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{2}\.\d{2})", text)
    if not match:
        return None
    raw = match.group(1)
    fmt = "%d.%m.%Y" if len(raw) == 10 else "%d.%m.%y"
    try:
        return datetime.strptime(raw, fmt).date()
    except ValueError:
        return None


def get_season_id(match_date: date) -> int:
    """Maç tarihine göre Transfermarkt sezon yılını (örn. 2026/27 için 2026) döndürür."""
    return match_date.year if match_date.month >= 7 else match_date.year - 1


@st.cache_data(ttl=3600, show_spinner=False)
def search_clubs(query: str) -> list[dict]:
    """Transfermarkt üzerinde kulüp araması yapar."""
    url = "https://www.transfermarkt.com.tr/schnellsuche/ergebnis/schnellsuche"
    resp = requests.get(url, params={"query": query}, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    clubs = []
    seen_ids = set()
    for a in soup.find_all("a", href=re.compile(r"^/([^/]+)/startseite/verein/(\d+)")):
        m = re.search(r"^/([^/]+)/startseite/verein/(\d+)", a["href"])
        if not m:
            continue
        slug, club_id = m.group(1), m.group(2)
        name = (a.get("title") or a.get_text(strip=True)).strip()
        if not name or club_id in seen_ids:
            continue
        seen_ids.add(club_id)
        clubs.append({"id": club_id, "slug": slug, "name": name})
    return clubs


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_season_schedule(club_id: str, club_slug: str, season_id: int) -> list[dict]:
    """Bir takımın ilgili sezondaki tüm kulvarlardaki fikstürünü kronolojik çeker."""
    time.sleep(0.35)
    url = f"https://www.transfermarkt.com.tr/{club_slug}/spielplandatum/verein/{club_id}/saison_id/{season_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.select_one("div.responsive-table table")
    if not table:
        return []

    matches = []
    current_comp = ""
    tbody = table.find("tbody") or table

    for row in tbody.find_all("tr", recursive=False):
        # Kulvar başlığı satırı kontrolü
        header_cell = row.find("td", attrs={"colspan": True})
        if header_cell and not row.find("a", href=re.compile(r"/verein/\d+")):
            comp_link = header_cell.find("a")
            current_comp = (
                comp_link.get("title") or comp_link.get_text(strip=True)
                if comp_link
                else header_cell.get_text(strip=True)
            )
            continue

        row_text = row.get_text(" ", strip=True)
        m_date = parse_tm_date(row_text)
        if not m_date:
            continue

        # Rakip kulüp bağlantısını bul
        opp_id, opp_name = None, None
        for a in row.find_all("a", href=re.compile(r"/([^/]+)/(?:spielplan|startseite)/verein/(\d+)")):
            m = re.search(r"/([^/]+)/(?:spielplan|startseite)/verein/(\d+)", a["href"])
            if m and m.group(2) != str(club_id):
                opp_id = m.group(2)
                opp_name = (a.get("title") or a.get_text(strip=True)).strip()
                if opp_name:
                    break

        if not opp_id or not opp_name:
            continue

        # Ev / Deplasman bilgisi
        venue = ""
        for td in row.find_all("td"):
            txt = td.get_text(strip=True)
            if txt in ("E", "D", "H", "A", "T"):
                venue = "Ev" if txt in ("E", "H") else ("Dep" if txt in ("D", "A") else "Tarafsız")
                break

        # Skor bilgisi
        score_match = re.search(r"\b(\d+:\d+)\b", row_text)
        score = score_match.group(1) if score_match else "-"

        matches.append(
            {
                "date": m_date,
                "opp_id": opp_id,
                "opp_name": opp_name,
                "venue": venue,
                "score": score,
                "comp": current_comp,
            }
        )

    matches.sort(key=lambda x: x["date"])
    return matches


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_h2h_matches(club_a_id: str, club_a_slug: str, club_b_id: str) -> list[dict]:
    """İki takım arasındaki tüm geçmiş ve planlanmış karşılaşmaları çeker."""
    time.sleep(0.35)
    url = f"https://www.transfermarkt.com.tr/{club_a_slug}/bilanzdetail/verein/{club_a_id}/gegner_id/{club_b_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.select_one("div.responsive-table table")
    if not table:
        return []

    h2h_list = []
    tbody = table.find("tbody") or table

    for row in tbody.find_all("tr", recursive=False):
        row_text = row.get_text(" ", strip=True)
        m_date = parse_tm_date(row_text)
        if not m_date:
            continue

        teams_in_row = []
        seen_row_ids = set()
        for a in row.find_all("a", href=re.compile(r"/([^/]+)/(?:spielplan|startseite)/verein/(\d+)")):
            m = re.search(r"/([^/]+)/(?:spielplan|startseite)/verein/(\d+)", a["href"])
            if m:
                tid = m.group(2)
                tname = (a.get("title") or a.get_text(strip=True)).strip()
                if tname and tid not in seen_row_ids:
                    seen_row_ids.add(tid)
                    teams_in_row.append({"id": tid, "name": tname})

        if len(teams_in_row) < 2:
            continue

        score_match = re.search(r"\b(\d+:\d+)\b", row_text)
        score = score_match.group(1) if score_match else "-:-"

        h2h_list.append(
            {
                "date": m_date,
                "home_id": teams_in_row[0]["id"],
                "home_name": teams_in_row[0]["name"],
                "away_id": teams_in_row[1]["id"],
                "away_name": teams_in_row[1]["name"],
                "score": score,
            }
        )

    h2h_list.sort(key=lambda x: x["date"], reverse=True)
    return h2h_list


def find_neighbors(schedule: list[dict], target_date: date, opp_id: str):
    """Fikstür listesinde hedef maçı bulur; bir önceki ve bir sonraki maçı döndürür."""
    idx = None
    # 1. Tam tarih + rakip ID eşleşmesi
    for i, m in enumerate(schedule):
        if m["opp_id"] == str(opp_id) and m["date"] == target_date:
            idx = i
            break
    # 2. Tarih kayması ihtimaline karşı ±4 gün toleranslı rakip eşleşmesi
    if idx is None:
        for i, m in enumerate(schedule):
            if m["opp_id"] == str(opp_id) and abs((m["date"] - target_date).days) <= 4:
                idx = i
                break
    # 3. Sadece tarih eşleşmesi
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
        st.session_state["clubs_a"] = search_clubs(team_a_query)
        st.session_state["clubs_b"] = search_clubs(team_b_query)

if "clubs_a" in st.session_state and "clubs_b" in st.session_state:
    clubs_a = st.session_state["clubs_a"]
    clubs_b = st.session_state["clubs_b"]

    if not clubs_a or not clubs_b:
        st.error("Takımlardan biri bulunamadı. Lütfen takım adını kontrol edip tekrar deneyin.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            sel_a = st.selectbox(
                "1. Takımı Onayla",
                options=clubs_a,
                format_func=lambda x: f"{x['name']} (ID: {x['id']})",
            )
        with c2:
            sel_b = st.selectbox(
                "2. Takımı Onayla",
                options=clubs_b,
                format_func=lambda x: f"{x['name']} (ID: {x['id']})",
            )

        c3, c4 = st.columns(2)
        with c3:
            target_date = st.date_input("Analiz Edilecek Maç Tarihi", value=date(2026, 11, 21), format="DD.MM.YYYY")
        with c4:
            h2h_limit = st.slider("İncelenecek Geçmiş Maç Sayısı", min_value=1, max_value=15, value=5)

        show_details = st.checkbox("Rakiplerin yanına tarih ve Ev/Dep bilgisini ekle", value=True)

        if st.button("Analizi Başlat", type="primary"):
            with st.spinner("Fikstürler ve geçmiş karşılaşmalar taranıyor..."):
                # 1. Hedef maç için güncel sezon fikstürleri
                target_season = get_season_id(target_date)
                sched_a_curr = fetch_season_schedule(sel_a["id"], sel_a["slug"], target_season)
                sched_b_curr = fetch_season_schedule(sel_b["id"], sel_b["slug"], target_season)

                prev_a, next_a = find_neighbors(sched_a_curr, target_date, sel_b["id"])
                prev_b, next_b = find_neighbors(sched_b_curr, target_date, sel_a["id"])

                # Çıktı metnini oluştur
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

                # Excel aktarımı için tablo satırları
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

                # 2. Geçmiş karşılaşmalar (H2H)
                all_h2h = fetch_h2h_matches(sel_a["id"], sel_a["slug"], sel_b["id"])
                past_h2h = [m for m in all_h2h if m["date"] < target_date][:h2h_limit]

                if not past_h2h:
                    lines.append("Bu tarihten önce oynanmış karşılaşma bulunamadı.")
                else:
                    for h in past_h2h:
                        h_date = h["date"]
                        h_season = get_season_id(h_date)
                        h_date_str = h_date.strftime("%d.%m.%Y")
                        match_title = f"{h_date_str} | {h['home_name']} {h['score']} {h['away_name']}"

                        s_a = fetch_season_schedule(sel_a["id"], sel_a["slug"], h_season)
                        s_b = fetch_season_schedule(sel_b["id"], sel_b["slug"], h_season)

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

                # Excel İndirme Butonu
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
