
import io
import os
import re
import json
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

st.set_page_config(page_title="🔒 Szabadulószoba – Excel vezérléssel (lokális)", layout="centered")

st.title("🔒 Szabadulószoba – Excel vezérléssel (lokális)")
st.caption("Szobák = Excel munkalapok • Kérdések & megoldások Excelből • Hibás válasz → zárolás")

STATE_FILE = Path("escape_state.json")  # perzisztens állapot (helyi mappa)

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
        cols = {str(c).lower(): c for c in df.columns}
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
        df["Kérdés"] = df["Kérdés"].astype(str).str.strip()
        df["Megoldás"] = df["Megoldás"].astype(str).str.strip()
        if "LezárásPerc" in df.columns:
            df["LezárásPerc"] = pd.to_numeric(df["LezárásPerc"], errors="coerce").fillna(0).clip(lower=0).astype(int)
        else:
            df["LezárásPerc"] = 0
        if "Hint" not in df.columns:
            df["Hint"] = ""
        df = df.dropna(subset=["Kérdés","Megoldás"]).reset_index(drop=True)
        if not df.empty:
            data[s] = df[["Kérdés","Megoldás","LezárásPerc","Hint"]]
    return sheets, data

def get_progress(state: Dict, team_id: str, room: str) -> Dict:
    key = f"{team_id}::{room}"
    return state.get(key, {"idx": 0, "lock_until": 0.0})

def set_progress(state: Dict, team_id: str, room: str, idx: int = None, lock_until: float = None) -> None:
    key = f"{team_id}::{room}"
    entry = state.get(key, {"idx": 0, "lock_until": 0.0})
    if idx is not None:
        entry["idx"] = idx
    if lock_until is not None:
        entry["lock_until"] = float(lock_until)
    state[key] = entry

def format_mmss(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m:02d}:{s:02d}"

# ---------- Oldalsáv ----------
with st.sidebar:
    st.header("🏁 Csapat / Bemenet")
    team_id = st.text_input("Csapat azonosító", value="csapat1", help="Minden csapatnak adj egyedi ID-t.")
    uploaded = st.file_uploader("Excel (.xlsx) feltöltése", type=["xlsx"])
    st.caption("Szobák = munkalapok • Oszlopok: **Kérdés**, **Megoldás**, (opcionális) **LezárásPerc**, **Hint**.")
    st.download_button(
        "📥 Minta Excel letöltése",
        data=st.session_state.get("_template_bytes", b""),
        file_name="escape_rooms_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Kezdő sablon két szobával"
    )
    st.divider()

    st.header("🧑‍💼 Játékmester mód")
    pw_env = os.environ.get("ESCAPE_ADMIN_PW", "admin")
    admin_try = st.text_input("Jelszó (alapértelmezett: admin)", type="password")
    admin = admin_try == pw_env
    if admin:
        st.success("Játékmester mód aktív.")
        if st.button("♻️ Minden haladás/zárolás törlése ennél a csapatnál"):
            state = load_state()
            state = {k:v for k,v in state.items() if not k.startswith(f"{team_id}::")}
            save_state(state)
            st.info("Törölve.")
        reset_room = st.text_input("Szoba törlése (munkalap neve)")
        if st.button("🧼 Töröld ezt a szobát a csapatnál"):
            if reset_room:
                state = load_state()
                key = f"{team_id}::{reset_room}"
                if key in state:
                    state.pop(key)
                    save_state(state)
                    st.info(f"Törölve: {key}")
                else:
                    st.warning("Nem volt ilyen bejegyzés.")
    else:
        st.info("Add meg az admin jelszót a haladás törléséhez. (Állítsd be ENV: ESCAPE_ADMIN_PW)")

# Minta Excel a memóriában (letöltéshez)
if "_template_bytes" not in st.session_state:
    df1 = pd.DataFrame({
        "Kérdés": [
            "Írd be az első titkos szót (tipp: rejtjel a falon)",
            "Mi a kulcsszó? (több jó: piros vagy kék)",
            "Számold ki: 7*6"
        ],
        "Megoldás": [
            "re:alpha\\d{2}",     # regex példa
            "piros|kék",          # alternatívák
            "42"                  # pontos egyezés
        ],
        "LezárásPerc": [1, 2, 1],
        "Hint": ["betűk + 2 számjegy", "színek", "egyszerű szorzás"]
    })
    df2 = pd.DataFrame({
        "Kérdés": ["Másik szoba első kérdése"],
        "Megoldás": ["titok"],
        "LezárásPerc": [2],
        "Hint": ["nézz körül a polcon"]
    })
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df1.to_excel(writer, sheet_name="Szoba A", index=False)
        df2.to_excel(writer, sheet_name="Szoba B", index=False)
    buf.seek(0)
    st.session_state["_template_bytes"] = buf.read()

if not uploaded:
    st.info("Tölts fel egy Excel fájlt a bal oldali sávban, vagy töltsd le a mintát és töltsd vissza.")
    st.stop()

xls_bytes = uploaded.read()
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
row_lock = int(row.get("LezárásPerc", 0))
hint = row.get("Hint", "")

if locked:
    st.error(f"⛔ Hibás válasz. Zárolva még **{(remaining//60):02d}:{(remaining%60):02d}** ideig.")
    total_secs = max(1, (row_lock if row_lock else 0) * 60)
    if total_secs > 0:
        st.progress(max(0.0, min(1.0, 1 - remaining / total_secs)))
    if st.button("🔁 Frissítés"):
        try:
            st.rerun()
        except Exception:
            st.experimental_rerun()
    st.stop()

st.info(question)
if hint:
    with st.expander("💡 Segítség"):
        st.write(hint)

with st.form(key="answer_form", clear_on_submit=False):
    user_answer = st.text_input("Válasz", help="Szöveg, szám, több megoldás is lehet (pl. 'piros|kék'). Regex: 're:...'")
    submit = st.form_submit_button("Ellenőrzés")

if submit:
    ok = answer_matches(user_answer, answer_key)
    if ok:
        set_progress(state, team_id, chosen, idx=idx+1)  # következő kérdés
        save_state(state)
        st.success("✅ Helyes! Következő feladat...")
        try:
            st.rerun()
        except Exception:
            st.experimental_rerun()
    else:
        minutes = row_lock if row_lock and row_lock > 0 else 3
        lock_until = now + minutes * 60
        set_progress(state, team_id, chosen, lock_until=lock_until)
        save_state(state)
        st.error(f"⛔ Hibás! A próbálkozás **{minutes} percig** zárolva van.")
        try:
            st.rerun()
        except Exception:
            st.experimental_rerun()
