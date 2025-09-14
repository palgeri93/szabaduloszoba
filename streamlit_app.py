
import io
import os
import re
import json
import time
import html
import unicodedata
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

st.set_page_config(page_title="🔒 Szabadulószoba – Excel vezérléssel", layout="centered")

st.title("🔒 Szabadulószoba – Excel vezérléssel")
st.caption("Szobák = Excel munkalapok • Kérdés–válasz Excelből • Hibás válasz → zárolás (élő visszaszámlálással)")

STATE_FILE = Path("escape_state.json")  # perzisztens állapot (helyi mappa)
LOCAL_TEMPLATE = Path("escape_rooms_template.xlsx")  # automatikus forrás, ha létezik

# ---------- Segédfüggvények ----------
def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: Dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def now_ts() -> float:
    return time.time()

def normalize(s: str) -> str:
    """Kisbetű, szóköz trim, ékezetek eltávolítása."""
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = " ".join(s.split())
    return s

def answer_matches(user: str, key: str) -> bool:
    """Egyezés: 're:...' regex, vagy 'a|b|c' alternatívák, különben pontos egyezés normalizálva."""
    if key is None:
        return False
    key = str(key).strip()
    if key.startswith("re:"):
        pattern = key[3:].strip()
        try:
            return re.fullmatch(pattern, user or "", flags=re.IGNORECASE) is not None
        except re.error:
            pass
    if "|" in key:
        alts = [k.strip() for k in key.split("|")]
        return normalize(user) in {normalize(k) for k in alts}
    return normalize(user) == normalize(key)

@st.cache_data(show_spinner=False)
def read_workbook(xls_bytes: bytes) -> Tuple[List[str], Dict[str, pd.DataFrame]]:
    xls = pd.ExcelFile(io.BytesIO(xls_bytes))
    sheets = xls.sheet_names
    data = {}
    for s in sheets:
        df = pd.read_excel(io.BytesIO(xls_bytes), sheet_name=s)
        # várjuk: Kérdés, Megoldás (opcionális: LezárásPerc, Hint)
        cols = {str(c).strip().lower(): c for c in df.columns}
        def pick(*names):
            for n in names:
                if n.lower() in cols:
                    return cols[n.lower()]
            return None
        qcol = pick("Kérdés", "Kerdes", "Question", "Feladat")
        acol = pick("Megoldás", "Megoldas", "Answer", "Válasz", "Valasz")
        lcol = pick("LezárásPerc", "LezarasPerc", "LockMinutes")
        hcol = pick("Hint", "Segítség", "Segitseg")

        if qcol is None or acol is None:
            continue

        df = df.rename(columns={qcol:"Kérdés", acol:"Megoldás"})
        if lcol: df = df.rename(columns={lcol:"LezárásPerc"})
        if hcol: df = df.rename(columns={hcol:"Hint"})

        # tisztítás + zárolási percek (float is lehet, pl. 0,5 vagy "0,5")
        df["Kérdés"] = df["Kérdés"].astype(str).str.strip()
        df["Megoldás"] = df["Megoldás"].astype(str).str.strip()

        if "LezárásPerc" in df.columns:
            def to_minutes(v):
                if pd.isna(v): return 0.0
                if isinstance(v, str):
                    v = v.replace(",", ".").strip()
                try:
                    return max(0.0, float(v))
                except Exception:
                    return 0.0
            df["LezárásPerc"] = df["LezárásPerc"].apply(to_minutes)
        else:
            df["LezárásPerc"] = 0.0

        if "Hint" not in df.columns:
            df["Hint"] = ""

        df = df.dropna(subset=["Kérdés","Megoldás"]).reset_index(drop=True)
        if not df.empty:
            data[s] = df[["Kérdés","Megoldás","LezárásPerc","Hint"]]
    return sheets, data

def get_progress(state: Dict, team_id: str, room: str) -> Dict:
    key = f"{team_id}::{room}"
    return state.get(key, {"idx": 0, "lock_until": 0.0, "lock_total": 0.0})

def set_progress(state: Dict, team_id: str, room: str, idx: int = None, lock_until: float = None, lock_total: float = None) -> None:
    key = f"{team_id}::{room}"
    entry = state.get(key, {"idx": 0, "lock_until": 0.0, "lock_total": 0.0})
    if idx is not None:
        entry["idx"] = idx
    if lock_until is not None:
        entry["lock_until"] = float(lock_until)
    if lock_total is not None:
        entry["lock_total"] = float(lock_total)
    state[key] = entry

def format_mmss(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m:02d}:{s:02d}"

# ---------- Oldalsáv ----------
with st.sidebar:
    st.header("🏁 Csapat / Forrás")
    team_id = st.text_input("Csapat azonosító", value="csapat1", help="Minden csapatnak adj egyedi ID-t.")

    uploaded = st.file_uploader("Excel (.xlsx) feltöltése", type=["xlsx"])
    if uploaded is not None:
        xls_bytes = uploaded.read()
        st.caption("Forrás: feltöltött Excel.")
    else:
        # Automatikus alapértelmezés: helyi fájl, ha létezik
        if LOCAL_TEMPLATE.exists():
            xls_bytes = LOCAL_TEMPLATE.read_bytes()
            st.caption(f"Forrás: helyi fájl – {LOCAL_TEMPLATE.name}")
        else:
            xls_bytes = None
            st.info("Tölts fel egy Excel fájlt, vagy helyezd el a mappában: 'escape_rooms_template.xlsx'.")

    st.divider()
    st.caption("Oszlopok: **Kérdés**, **Megoldás**, (opcionális) **LezárásPerc** (percben, pl. 0,5), **Hint**.")

if not xls_bytes:
    st.stop()

sheets, data = read_workbook(xls_bytes)
rooms = [s for s in sheets if s in data]
if not rooms:
    st.error("Nem találtam érvényes munkalapot (hiányzik a 'Kérdés' és 'Megoldás' oszlop).")
    st.stop()

# ---------- Szobaválasztó gombok ----------
st.subheader("Szobák")
chosen = st.session_state.get("room")
cols = st.columns(4)
for i, r in enumerate(rooms):
    if cols[i % 4].button(r):
        st.session_state["room"] = r
        chosen = r

if not chosen:
    chosen = rooms[0]
    st.session_state["room"] = chosen

st.success(f"Kiválasztva: **{chosen}**")

# ---------- Játék logika ----------
state = load_state()
progress = get_progress(state, team_id, chosen)
idx = int(progress.get("idx", 0))
lock_until = float(progress.get("lock_until", 0.0))
lock_total = float(progress.get("lock_total", 0.0))  # másodpercben eltárolva (a pontos visszaszámláláshoz)
now = now_ts()
locked = now < lock_until
remaining = int(lock_until - now)

df = data[chosen]
total = len(df)

st.write(f"Feladat: **{idx+1} / {total}**")

if idx >= total:
    st.success("🎉 Készen vagytok ezzel a szobával!")
    st.stop()

row = df.iloc[idx]
question = row["Kérdés"]
answer_key = row["Megoldás"]
row_lock_min = float(row.get("LezárásPerc", 0.0))  # perc (float is lehet, pl. 0.5)
row_lock_secs = int(round(row_lock_min * 60.0))

# --------- KIJELZŐ: Kérdés (fekete, nagyobb betű) ---------
st.markdown(
    f"""
<div style="font-size:22px; color:#000; font-weight:600; line-height:1.45; border:1px solid #e5e7eb; padding:12px 14px; border-radius:10px; background:#fff;">
  {html.escape(question)}
</div>
""",
    unsafe_allow_html=True
)

# --------- HA ZÁROLVA VAN: élő visszaszámláló ---------
if locked:
    st.error("⛔ Hibás válasz. Zárolva, amíg a visszaszámláló 0-ra ér.")
    # ha korábban eltároltuk a teljes zárolási időt, azt használjuk; különben esünk vissza a sor szerinti percekre (vagy 180s)
    total_secs = int(lock_total) if lock_total > 0 else (row_lock_secs if row_lock_secs > 0 else 180)
    total_secs = max(total_secs, 1)

    pb = st.progress(0.0)
    timer_ph = st.empty()

    # Frissítjük másodpercenként
    target = lock_until
    while True:
        now2 = now_ts()
        rem = int(target - now2)
        if rem <= 0:
            pb.progress(1.0)
            timer_ph.markdown("**00:00**")
            # feloldás és újrarenderelés
            try:
                st.rerun()
            except Exception:
                st.experimental_rerun()
            st.stop()
        # százalék a teljes időhöz viszonyítva
        pct = max(0.0, min(1.0, 1 - rem / total_secs))
        pb.progress(pct)
        timer_ph.markdown(f"**{rem//60:02d}:{rem%60:02d}**")
        time.sleep(1)

# --------- Válasz űrlap ---------
with st.form(key="answer_form", clear_on_submit=False):
    user_answer = st.text_input("Válasz", help="Szöveg, szám, több megoldás is lehet (pl. 'piros|kék'). Regex: 're:...'")
    submit = st.form_submit_button("Ellenőrzés")

if submit:
    ok = answer_matches(user_answer, answer_key)
    if ok:
        set_progress(state, team_id, chosen, idx=idx+1, lock_until=0.0, lock_total=0.0)
        save_state(state)
        st.success("✅ Helyes! Következő feladat...")
        try:
            st.rerun()
        except Exception:
            st.experimental_rerun()
    else:
        # zárolás: a sor 'LezárásPerc' értéke (perc → másodperc). Ha nincs/0, legyen 3 perc.
        minutes = row_lock_min if row_lock_min and row_lock_min > 0 else 3.0
        total_secs = int(round(minutes * 60.0))
        until = now + total_secs
        set_progress(state, team_id, chosen, lock_until=until, lock_total=total_secs)
        save_state(state)
        st.error(f"⛔ Hibás! A próbálkozás **{minutes} percig** zárolva van.")
        try:
            st.rerun()
        except Exception:
            st.experimental_rerun()
