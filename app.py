import io, unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import pandas as pd, requests, streamlit as st

st.set_page_config(page_title="1/2 & 2/1 Rakip Analizi", page_icon="⚽", layout="centered")
HDR = {"User-Agent": "Mozilla/5.0"}
TR_TZ = timezone(timedelta(hours=3))
API = "https://site.api.espn.com/apis/site/v2/sports/soccer"
norm = lambda s: "".join(c for c in unicodedata.normalize("NFD", s.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")).lower().strip()) if unicodedata.category(c) != "Mn")
season_of = lambda d: d.year if d.month >= 7 else d.year - 1
to_int = lambda v: int(float(v)) if v not in (None, "") and str(v).lstrip("-").replace(".", "", 1).isdigit() else None

@st.cache_data(ttl=86400, show_spinner=False)
def get_tr_teams():
    teams, seen = [], set()
    for lg in ("tur.1", "tur.2"):
        try:
            for sp in requests.get(f"{API}/{lg}/teams", headers=HDR, timeout=12).json().get("sports", []):
                for lo in sp.get("leagues", []):
                    for it in lo.get("teams", []):
                        t = it.get("team", {})
                        tid, nm = str(t.get("id", "")), t.get("displayName") or t.get("name", "")
                        if tid and nm and tid not in seen:
                            seen.add(tid)
                            teams.append({"id": tid, "name": nm, "league": lg})
        except Exception:
            pass
    return teams

@st.cache_data(ttl=3600, show_spinner=False)
def search_clubs(q: str):
    q = q.strip()
    if not q:
        return [], "Takım adı boş."
    nq, res, seen = norm(q), [], set()
    for t in get_tr_teams():
        if nq in norm(t["name"]):
            seen.add(t["id"])
            res.append(t)
    try:
        for it in requests.get("https://site.web.api.espn.com/apis/common/v3/search", params={"query": q, "limit": 20, "type": "team"}, headers=HDR, timeout=12).json().get("items", []):
            if it.get("sport") == "soccer" or "s:600" in it.get("uid", ""):
                tid, nm = str(it.get("id", "")), it.get("displayName") or it.get("name", "")
                if tid and nm and tid not in seen:
                    seen.add(tid)
                    res.append({"id": tid, "name": nm, "league": it.get("league", "all")})
    except Exception as e:
        if not res:
            return [], f"Bağlantı hatası: {e}"
    return (res, None) if res else ([], "Takım bulunamadı.")

def ht_comp(cs):
    h = a = None
    for c in cs:
        ls = c.get("linescores") or []
        if ls:
            v = to_int(ls[0].get("value") if isinstance(ls[0], dict) else ls[0])
            if c.get("homeAway") == "home":
                h = v
            elif c.get("homeAway") == "away":
                a = v
    return h, a

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_ht(eid: str, lg: str, hid: str, aid: str):
    if not eid or not eid.isdigit():
        return None, None
    for u in ([f"{API}/{lg}/summary"] if lg and lg != "all" else []) + [f"{API}/all/summary"]:
        try:
            d = requests.get(u, params={"event": eid}, headers=HDR, timeout=10).json()
            hc = d.get("header", {}).get("competitions", [])
            if hc:
                hh, ha = ht_comp(hc[0].get("competitors", []))
                if hh is not None and ha is not None:
                    return hh, ha
                dets = [x for x in hc[0].get("details", []) if x.get("scoringPlay")]
                if dets:
                    p1 = [str(x.get("team", {}).get("id", "")) for x in dets if x.get("period", {}).get("number") == 1]
                    return p1.count(str(hid)), p1.count(str(aid))
            sps = d.get("scoringPlays", [])
            if sps:
                p1 = [str(x.get("team", {}).get("id", "")) for x in sps if x.get("period", {}).get("number") == 1]
                return p1.count(str(hid)), p1.count(str(aid))
        except Exception:
            pass
    return None, None

def parse_ev(evs, cid):
    out = []
    for ev in evs:
        try:
            md = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TR_TZ).date()
        except Exception:
            continue
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        hs = next((c for c in cs if c.get("homeAway") == "home"), None)
        as_ = next((c for c in cs if c.get("homeAway") == "away"), None)
        ms = next((c for c in cs if str(c.get("team", {}).get("id")) == str(cid)), None)
        os_ = next((c for c in cs if str(c.get("team", {}).get("id")) != str(cid)), None)
        if not (hs and as_ and ms and os_):
            continue
        ot, ht, at = os_["team"], hs["team"], as_["team"]
        oid, onm = str(ot.get("id", "")), ot.get("displayName") or ot.get("shortDisplayName", "")
        if not (oid and onm):
            continue
        done = bool(comp.get("status", {}).get("type", {}).get("completed"))
        sc = lambda s: to_int(s["score"].get("value", s["score"].get("displayValue")) if isinstance(s.get("score"), dict) else s.get("score"))
        hth, hta = ht_comp(cs) if done else (None, None)
        out.append({"eid": str(ev.get("id", "")), "lg": (ev.get("league") or {}).get("slug", "") if isinstance(ev.get("league"), dict) else "", "date": md, "done": done, "oid": oid, "onm": onm, "ven": "Ev" if ms.get("homeAway") == "home" else "Dep", "hid": str(ht.get("id", "")), "hnm": ht.get("displayName") or ht.get("shortDisplayName", ""), "aid": str(at.get("id", "")), "anm": at.get("displayName") or at.get("shortDisplayName", ""), "fh": sc(hs) if done else None, "fa": sc(as_) if done else None, "hh": hth, "ha": hta})
    return out

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sched(cid: str, lg: str, yr: int):
    merged = {}
    for u in [f"{API}/all/teams/{cid}/schedule"] + ([f"{API}/{lg}/teams/{cid}/schedule"] if lg and lg != "all" else []):
        try:
            for m in parse_ev(requests.get(u, params={"season": yr}, headers=HDR, timeout=12).json().get("events", []), cid):
                merged.setdefault(f"{m['date']}_{m['oid']}", m)
        except Exception:
            pass
    return sorted(merged.values(), key=lambda x: x["date"])

def neighbors(sched, td, oid):
    idx = next((i for i, m in enumerate(sched) if m["oid"] == str(oid) and m["date"] == td), None)
    if idx is None:
        idx = next((i for i, m in enumerate(sched) if m["oid"] == str(oid) and abs((m["date"] - td).days) <= 5), None)
    if idx is None:
        c = sorted((abs((m["date"] - td).days), i) for i, m in enumerate(sched) if m["oid"] == str(oid))
        idx = c[0][1] if c else next((i for i, m in enumerate(sched) if m["date"] == td), None)
    return (None, None) if idx is None else (sched[idx - 1] if idx > 0 else None, sched[idx + 1] if idx < len(sched) - 1 else None)

def iy_ms(m):
    fh, fa, hh, ha = m.get("fh"), m.get("fa"), m.get("hh"), m.get("ha")
    ms = f"{fh}-{fa}" if fh is not None and fa is not None else "-:-"
    iy = f"{hh}-{ha}" if hh is not None and ha is not None else "-:-"
    if None in (fh, fa, hh, ha):
        return iy, ms, "Bilinmiyor"
    c = f"{'1' if hh > ha else ('2' if hh < ha else 'X')}/{'1' if fh > fa else ('2' if fh < fa else 'X')}"
    return iy, ms, ("1/2 (İY 1 -> MS 2)" if c == "1/2" else ("2/1 (İY 2 -> MS 1)" if c == "2/1" else f"Normal ({c})"))

def fmt(m, det):
    if not m:
        return "Yok (Sezon başı/sonu)"
    if not det:
        return m["onm"]
    sc = f" [{m['fh']}-{m['fa']}]" if m.get("fh") is not None else ""
    return f"{m['onm']} ({m['date'].strftime('%d.%m.%Y')} - {m['ven']}{sc})"

def eval_sim(t_op, c_op):
    slots = ["a_prev", "a_next", "b_prev", "b_next"]
    ti = {s: (t_op[s]["oid"] if t_op[s] else None) for s in slots}
    ci = {s: (c_op[s]["oid"] if c_op[s] else None) for s in slots}
    ts, cs = {v for v in ti.values() if v}, {v for v in ci.values() if v}
    nms = [c_op[s]["onm"] for s in slots if ci[s] and ci[s] in ts]
    ep, ov = sum(1 for s in slots if ci[s] and ci[s] == ti[s]), len(ts & cs)
    if len(ts) == 4 and ov == 4:
        return f"4/4 AYNI 4 TAKIM ({'Aynı Sıra' if ep == 4 else 'Farklı Sıra'})!"
    return f"{ov}/4 Benzer ({', '.join(dict.fromkeys(nms))})" if ov > 0 else "0/4 Farklı"

st.title("⚽ 1/2 & 2/1 Geri Dönüş ve Rakip Analizi")
st.caption("1/2 ve 2/1 maçlarını bulur, önceki/sonraki 4 rakibi karşılaştırır ve Excel çıktısı verir.")
c1, c2 = st.columns(2)
qa = c1.text_input("1. Takım Adı", "Galatasaray")
qb = c2.text_input("2. Takım Adı", "Samsunspor")

if st.button("Takımları Getir", type="secondary"):
    with st.spinner("Takımlar aranıyor..."):
        ca, ea = search_clubs(qa)
        cb, eb = search_clubs(qb)
        st.session_state.update({"ca": ca, "cb": cb, "ea": ea, "eb": eb})

if "ca" in st.session_state and "cb" in st.session_state:
    ca, cb = st.session_state["ca"], st.session_state["cb"]
    if not ca or not cb:
        if st.session_state.get("ea"):
            st.error(f"1. Takım: {st.session_state['ea']}")
        if st.session_state.get("eb"):
            st.error(f"2. Takım: {st.session_state['eb']}")
    else:
        s1, s2 = st.columns(2)
        sa = s1.selectbox("1. Takımı Onayla", ca, format_func=lambda x: f"{x['name']} ({x['league'].upper()})")
        sb = s2.selectbox("2. Takımı Onayla", cb, format_func=lambda x: f"{x['name']} ({x['league'].upper()})")
        s3, s4 = st.columns(2)
        td = s3.date_input("Analiz Edilecek Maç Tarihi", date(2026, 11, 21), format="DD.MM.YYYY")
        syrs = s4.slider("Taranacak Geçmiş Sezon", 5, 25, 15)
        o1, o2 = st.columns(2)
        only_cb = o1.checkbox("Sadece 1/2 ve 2/1 maçlarını çıkar", True)
        det = o2.checkbox("Tarih, Ev/Dep ve skor ekle", True)

        if st.button("Analizi Başlat", type="primary"):
            with st.spinner("Fikstürler ve ilk yarı (İY) skorları taranıyor..."):
                ty = season_of(td)
                yrs = list(range(ty, ty - syrs, -1))
                sam, sbm = {}, {}
                with ThreadPoolExecutor(max_workers=6) as pool:
                    for y, a_l, b_l in pool.map(lambda y: (y, fetch_sched(sa["id"], sa["league"], y), fetch_sched(sb["id"], sb["league"], y)), yrs):
                        sam[y], sbm[y] = a_l, b_l

                pa, na = neighbors(sam.get(ty, []), td, sb["id"])
                pb, nb = neighbors(sbm.get(ty, []), td, sa["id"])
                topps = {"a_prev": pa, "a_next": na, "b_prev": pb, "b_next": nb}

                tm = next((dict(m) for m in sam.get(ty, []) if m["oid"] == str(sb["id"]) and abs((m["date"] - td).days) <= 5), None)
                if tm and tm["done"]:
                    if tm["hh"] is None:
                        tm["hh"], tm["ha"] = fetch_ht(tm["eid"], tm["lg"] or sa["league"], tm["hid"], tm["aid"])
                    tiy, tms, tsc = iy_ms(tm)
                else:
                    tiy, tms, tsc = "-:-", "-:-", "Hedef Maç (Referans)"

                raw, sd = [], set()
                for y in yrs:
                    for m in reversed(sam.get(y, [])):
                        if m["oid"] == str(sb["id"]) and m["date"] < td and m["done"] and m["date"].isoformat() not in sd:
                            sd.add(m["date"].isoformat())
                            raw.append((y, dict(m)))

                def enr_fn(it):
                    y, m = it
                    if m["hh"] is None or m["ha"] is None:
                        m["hh"], m["ha"] = fetch_ht(m["eid"], m["lg"] or sa["league"], m["hid"], m["aid"])
                    return (y, m, *iy_ms(m))

                enr = list(ThreadPoolExecutor(max_workers=8).map(enr_fn, raw)) if raw else []
                flt = [x for x in enr if not only_cb or ("1/2" in x[4] or "2/1" in x[4])]

                excel_rows = [{
                    "Tür": "Hedef Maç", "Tarih": td.strftime("%d.%m.%Y"),
                    "Karşılaşma": f"{sa['name']} - {sb['name']}", "İY": tiy, "MS": tms, "Senaryo": tsc,
                    f"{sa['name']} Önceki": fmt(pa, det), f"{sa['name']} Sonraki": fmt(na, det),
                    f"{sb['name']} Önceki": fmt(pb, det), f"{sb['name']} Sonraki": fmt(nb, det),
                    "4 Rakip Benzerliği": "Referans 4 Rakip",
                }]
                lines = [
                    f"Hedef Maç: {td.strftime('%d.%m.%y')} {sa['name']} - {sb['name']} (İY: {tiy} | MS: {tms})",
                    f"  • {sa['name']}: {fmt(pa, det)} | {fmt(na, det)}",
                    f"  • {sb['name']}: {fmt(pb, det)} | {fmt(nb, det)}",
                    f"\nBulunan Maçlar (Taranan: {len(raw)}, Listelenen: {len(flt)}):", "=" * 50,
                ]
                cnt, rmap, sets = Counter(), {}, []
                tset = frozenset(v["oid"] for v in topps.values() if v)
                if len(tset) == 4:
                    sets.append((f"Hedef ({td.strftime('%d.%m.%Y')})", tset, topps))

                for hs, h, iys, mss, scs in flt:
                    hd = h["date"]
                    hds, mt = hd.strftime("%d.%m.%Y"), f"{h['hnm']} - {h['anm']}"
                    p_a, n_a = neighbors(sam.get(hs, []), hd, sb["id"])
                    p_b, n_b = neighbors(sbm.get(hs, []), hd, sa["id"])
                    copps = {"a_prev": p_a, "a_next": n_a, "b_prev": p_b, "b_next": n_b}
                    sim_txt = eval_sim(topps, copps)
                    for mo, rl in [(p_a, f"{hds} ({sa['name']} Önceki)"), (n_a, f"{hds} ({sa['name']} Sonraki)"), (p_b, f"{hds} ({sb['name']} Önceki)"), (n_b, f"{hds} ({sb['name']} Sonraki)")]:
                        if mo:
                            cnt[mo["onm"]] += 1
                            rmap.setdefault(mo["onm"], []).append(rl)
                    cs = frozenset(v["oid"] for v in copps.values() if v)
                    if len(cs) == 4:
                        sets.append((f"{hds} {mt} [{scs}]", cs, copps))
                    excel_rows.append({
                        "Tür": "Geri Dönüş (1/2)" if "1/2" in scs else ("Geri Dönüş (2/1)" if "2/1" in scs else "Geçmiş Maç"),
                        "Tarih": hds, "Karşılaşma": mt, "İY": iys, "MS": mss, "Senaryo": scs,
                        f"{sa['name']} Önceki": fmt(p_a, det), f"{sa['name']} Sonraki": fmt(n_a, det),
                        f"{sb['name']} Önceki": fmt(p_b, det), f"{sb['name']} Sonraki": fmt(n_b, det),
                        "4 Rakip Benzerliği": sim_txt,
                    })
                    lines += [
                        f"\n• {hds} | {mt} | İY: {iys} / MS: {mss} -> [{scs}]",
                        f"  {sa['name']}: {fmt(p_a, det)} | {fmt(n_a, det)}",
                        f"  {sb['name']}: {fmt(p_b, det)} | {fmt(n_b, det)}",
                        f"  4 Rakip Benzerliği: {sim_txt}",
                    ]

                id_pairs = [
                    f"AYNI 4 TAKIM: {sets[i][0]} <==> {sets[j][0]} ({', '.join(v['onm'] for v in sets[i][2].values() if v)})"
                    for i in range(len(sets)) for j in range(i + 1, len(sets)) if sets[i][1] == sets[j][1]
                ]
                rep = [(k, v, rmap[k]) for k, v in cnt.most_common() if v >= 2]

                m1, m2, m3 = st.columns(3)
                m1.metric("Taranan Maç", len(raw))
                m2.metric("Bulunan 1/2 - 2/1", len(flt))
                m3.metric("4/4 Aynı Set", len(id_pairs))
                for ip in id_pairs:
                    st.success(f"🎯 {ip}")
                if rep:
                    with st.expander("🔁 Tekrar Eden Çevre Rakipler", expanded=True):
                        for tn, cv, rls in rep:
                            st.write(f"**{tn}** ({cv} kez): {' • '.join(rls)}")

                df_main = pd.DataFrame(excel_rows)
                st.subheader("Özet Tablo")
                st.dataframe(df_main, use_container_width=True, hide_index=True)
                st.subheader("Metin Çıktısı (Sağ üstten tek tıkla kopyala)")
                st.code("\n".join(lines), language=None)

                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                    df_main.to_excel(writer, index=False, sheet_name="1-2_2-1_Analizi")
                    rep_rows = [{"Rakip Takım": tn, "Tekrar": f"{cv} kez", "Maç ve Pozisyonlar": " | ".join(rls)} for tn, cv, rls in rep] or [{"Rakip Takım": "Tekrar eden rakip yok", "Tekrar": "-", "Maç ve Pozisyonlar": "-"}]
                    pd.DataFrame(rep_rows).to_excel(writer, index=False, sheet_name="Tekrar_Eden_Rakipler")
                st.download_button(
                    "📥 Excel Olarak İndir (.xlsx)",
                    data=buf.getvalue(),
                    file_name=f"{sa['name']}_{sb['name']}_1-2_2-1_analiz.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
