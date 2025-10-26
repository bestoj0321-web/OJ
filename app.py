# app.py
# -------------------------------------------------------------
# 사내 테니스코트 예약 (A/B)
# - 3개 고정 시간대 (점심A/B, 퇴근 후 17:00~18:00)
# - Google Sheets 저장소 + 날짜별 부분 업데이트
# - Optimistic Concurrency(버전) + Best-effort Lock(만료 포함)
# -------------------------------------------------------------

from datetime import datetime, date
from typing import Dict, Any, List, Tuple

import pandas as pd
import streamlit as st

from store_with_lock import GoogleSheetsStoreWithLocks

st.set_page_config(
    page_title="사내 테니스코트 예약 (A/B)", page_icon="🎾", layout="centered"
)

# ----------------------
# Constants (Fixed Slots)
# ----------------------
BLOCKS = [
    {"id": "LUNCHA", "label": "점심시간 A", "start": "11:30", "end": "12:15"},
    {"id": "LUNCHB", "label": "점심시간 B", "start": "12:15", "end": "13:00"},
    {"id": "AFTER",  "label": "퇴근 후",     "start": "17:00", "end": "18:00"},
]
BLOCK_LOOKUP = {b["id"]: b for b in BLOCKS}

# 세션에 블록 정보 저장(스토어에서 초기화 시 사용)
st.session_state["_blocks"] = BLOCKS

# ----------------------
# Store (Google Sheets + Partial + Locks)
# ----------------------
@st.cache_resource
def get_store() -> GoogleSheetsStoreWithLocks:
    sheet_id = st.secrets["gsheet_id"]
    return GoogleSheetsStoreWithLocks(sheet_id, ws_resv="reservations", ws_vers="versions", ws_lock="locks")

store = get_store()

# ----------------------
# Helpers for in-memory day struct
# ----------------------
def ensure_day(day: Dict[str, Any]) -> Dict[str, Any]:
    # 보수적으로 A/B와 각 블록 키를 모두 보장
    if not day:
        day = {"A": {}, "B": {}}
    for c in ("A","B"):
        day.setdefault(c, {})
        for b in BLOCKS:
            day[c].setdefault(b["id"], None)
    return day


def book_block(day: Dict[str, Any], date_key: str, court: str, block_id: str, user: str, note: str) -> Tuple[bool, str]:
    """메모리 day에 반영만 수행. 저장은 별도(save_date)."""
    ensure_day(day)
    # 이미 해당 코트 동일 시간대 예약됨
    if day[court][block_id]:
        return False, "TAKEN"
    # 같은 시간대 다른 코트에 본인 예약 존재 (중복 방지)
    other = "B" if court == "A" else "A"
    if day[other][block_id] and day[other][block_id]["user"] == user:
        return False, "OVERLAP"
    day[court][block_id] = {
        "user": user,
        "note": (note or "").strip(),
        "createdAt": datetime.now().isoformat(timespec="seconds"),
    }
    return True, ""


def cancel_block(day: Dict[str, Any], date_key: str, court: str, block_id: str, user: str) -> Tuple[bool, str]:
    ensure_day(day)
    slot = day.get(court, {}).get(block_id)
    if not slot:
        return False, "NOT_FOUND"
    # 타인 예약 취소도 허용(두 번 클릭 UX는 store 레벨에서 구현 가능)
    day[court][block_id] = None
    return True, ""


def export_day_to_csv(db_day: Dict[str, Any], date_key: str) -> bytes:
    rows: List[Dict[str, Any]] = []
    for court in ("A", "B"):
        for block_id, slot in (db_day.get(court) or {}).items():
            if slot:
                b = BLOCK_LOOKUP.get(block_id, {"label": block_id, "start": "", "end": ""})
                rows.append(
                    {
                        "date": date_key,
                        "court": court,
                        "blockId": block_id,
                        "blockLabel": b.get("label"),
                        "start": b.get("start"),
                        "end": b.get("end"),
                        "user": slot.get("user"),
                        "note": slot.get("note", ""),
                        "createdAt": slot.get("createdAt", ""),
                    }
                )
    df = pd.DataFrame(rows)
    return df.to_csv(index=False).encode("utf-8-sig")

# ----------------------
# Sidebar – Settings
# ----------------------
with st.sidebar:
    st.header("⚙️ 설정")
    user_name = st.text_input("내 이름", value=st.session_state.get("user_name", ""), placeholder="예: 홍길동")
    if st.button("저장", use_container_width=True):
        st.session_state["user_name"] = user_name.strip()
        st.success("저장되었습니다.")

    st.divider()
    if st.button("모든 데이터 초기화(시트)", type="secondary", use_container_width=True):
        store.clear()
        st.success("초기화 완료")

# ----------------------
# Main – Tabs
# ----------------------
st.title("사내 테니스코트 예약 (A/B)")
st.caption("시간대 고정: 점심A(11:30~12:15) · 점심B(12:15~13:00) · 퇴근 후(17:00~18:00)")

TAB_RESERVE, TAB_MINE, TAB_EXPORT = st.tabs(["예약하기", "내 예약", "내보내기/관리"])

# ----------------------
# Atomic-ish ops using load→mutate→save(expected_version)
# ----------------------

def try_book(date_key: str, court: str, block_id: str, user: str, note: str):
    day, ver = store.load_date(date_key)
    day = ensure_day(day)
    ok, reason = book_block(day, date_key, court, block_id, user, note)
    if not ok:
        return False, reason
    ok2, reason2 = store.save_date(date_key=date_key, day=day, expected_version=ver, user=user, use_lock=True)
    return ok2, (reason2 or "")


def try_cancel(date_key: str, court: str, block_id: str, user: str):
    day, ver = store.load_date(date_key)
    day = ensure_day(day)
    ok, reason = cancel_block(day, date_key, court, block_id, user)
    if not ok:
        return False, reason
    ok2, reason2 = store.save_date(date_key=date_key, day=day, expected_version=ver, user=user, use_lock=True)
    return ok2, (reason2 or "")

# ----------------------
# Tab 1: Reserve
# ----------------------
with TAB_RESERVE:
    col1, col2 = st.columns([1, 1])
    with col1:
        sel_date: date = st.date_input("날짜", value=date.today(), format="YYYY-MM-DD")
    with col2:
        st.write("")
        st.write("")
        refresh = st.button("새로고침")

    date_key = sel_date.isoformat()

    # Load day (for current view)
    day_view, version = store.load_date(date_key)
    day_view = ensure_day(day_view)

    st.subheader(f"예약 현황 – {date_key}")

    def render_row(block: Dict[str, str]):
        st.markdown(f"**{block['label']}**  `{block['start']} ~ {block['end']}`")
        c1, c2 = st.columns(2)
        for i, court in enumerate(("A", "B")):
            with (c1 if i == 0 else c2):
                slot = day_view[court][block["id"]]
                if slot:
                    is_me = slot["user"] == st.session_state.get("user_name", "")
                    st.info(
                        f"**코트 {court}** · {slot['user']}" + (f" · {slot['note']}" if slot.get('note') else "")
                    )
                    if is_me:
                        if st.button(
                            f"취소 (코트 {court})",
                            key=f"cancel_{date_key}_{court}_{block['id']}",
                            use_container_width=True,
                        ):
                            ok, reason = try_cancel(date_key, court, block["id"], st.session_state.get("user_name", ""))
                            if ok:
                                st.rerun()
                            else:
                                st.error("취소 실패: " + reason)
                    else:
                        st.caption("타인 예약")
                else:
                    if not st.session_state.get("user_name"):
                        st.warning("설정에서 이름을 저장하세요.")
                    else:
                        note = st.text_input(
                            f"메모 ({court})",
                            key=f"note_{date_key}_{court}_{block['id']}",
                            placeholder="선택 사항",
                        )
                        if st.button(
                            f"예약 (코트 {court})",
                            key=f"book_{date_key}_{court}_{block['id']}",
                            use_container_width=True,
                        ):
                            ok, reason = try_book(date_key, court, block["id"], st.session_state["user_name"], note)
                            if ok:
                                st.success("예약 완료")
                                st.rerun()
                            else:
                                msg = (
                                    "이미 예약된 시간입니다." if reason == "TAKEN" else
                                    "동일 시간대에 본인 예약이 존재합니다." if reason == "OVERLAP" else
                                    "잠금을 획득하지 못했습니다. 잠시 후 다시 시도하세요." if reason == "LOCK_FAIL" else
                                    "다른 사용자가 먼저 변경했습니다. 새로고침 후 다시 시도하세요." if reason == "VERSION_CONFLICT" else
                                    f"예약 실패: {reason}"
                                )
                                st.error(msg)
        st.divider()

    for b in BLOCKS:
        render_row(b)

# ----------------------
# Tab 2: My Reservations
# ----------------------
with TAB_MINE:
    user = st.session_state.get("user_name", "")
    if not user:
        st.warning("설정에서 먼저 이름을 저장해주세요.")
    else:
        # 모든 날짜를 한번에 가져오지 않고, reservations 전체를 읽지 않는 대신
        # 간단히 최근 30일 정도만 훑는 최적화도 가능. 여기서는 예시로 오늘 날짜만 표시.
        day_view, _ = store.load_date(date.today().isoformat())
        items: List[Dict[str, Any]] = []
        for court in ("A", "B"):
            for block_id, slot in (day_view.get(court) or {}).items():
                if slot and slot.get("user") == user:
                    b = BLOCK_LOOKUP.get(block_id, {"label": block_id, "start": "", "end": ""})
                    items.append(
                        {
                            "date": date.today().isoformat(),
                            "court": court,
                            "blockId": block_id,
                            "label": b["label"],
                            "start": b["start"],
                            "end": b["end"],
                            "note": slot.get("note", ""),
                        }
                    )
        if not items:
            st.info("오늘 기준 다가오는 예약이 없습니다.")
        else:
            items.sort(key=lambda r: (r["date"], r["start"], r["court"]))
            for it in items:
                cols = st.columns([2, 1, 3, 1])
                cols[0].markdown(f"**{it['date']}**")
                cols[1].markdown(f"코트 **{it['court']}**")
                cols[2].markdown(f"{it['label']}  `{it['start']}~{it['end']}`" + (f" · {it['note']}" if it['note'] else ""))
                if cols[3].button(
                    "취소",
                    key=f"mine_cancel_{it['date']}_{it['court']}_{it['blockId']}",
                    use_container_width=True,
                ):
                    ok, reason = try_cancel(it["date"], it["court"], it["blockId"], user)
                    if ok:
                        st.success("취소 완료")
                        st.rerun()
                    else:
                        st.error("취소 실패: " + reason)

# ----------------------
# Tab 3: Export / Admin
# ----------------------
with TAB_EXPORT:
    col1, col2 = st.columns([1, 1])
    with col1:
        exp_date: date = st.date_input("다운로드할 날짜", value=date.today(), format="YYYY-MM-DD", key="exp_date")
    with col2:
        st.write("")
        st.write("")
        if st.button("해당 날짜 CSV 생성"):
            day_x, _ = store.load_date(exp_date.isoformat())
            csv_bytes = export_day_to_csv(day_x, exp_date.isoformat())
            st.session_state["_csv_ready"] = (csv_bytes, exp_date.isoformat())

    if "_csv_ready" in st.session_state:
        csv_bytes, dkey = st.session_state["_csv_ready"]
        st.download_button(
            label=f"CSV 다운로드 (tennis_{dkey}.csv)",
            data=csv_bytes,
            file_name=f"tennis_{dkey}.csv",
            mime="text/csv",
        )

st.caption("© Tennis Court A/B – Internal Use (Google Sheets 저장소 + Lock/OCC)")
