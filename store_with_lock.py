# store_with_lock.py
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional
import time
import uuid

import gspread
from google.oauth2.service_account import Credentials
import streamlit as st

HEADERS = ["date", "court", "blockId", "user", "note", "createdAt"]
VERS_HEADERS = ["date", "version"]
LOCK_HEADERS = ["date", "token", "user", "expiresAt"]

class GoogleSheetsStoreWithLocks:
    """
    날짜별 부분 업데이트 + 낙관적 동시성제어(version) + Best-effort 잠금(LOCK) 구현.
    - reservations: 실데이터
    - versions: date별 version (int)
    - locks: date별 락 (token, expiresAt)
    """
    def __init__(self, sheet_id: str, ws_resv="reservations", ws_vers="versions", ws_lock="locks"):
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
        gc = gspread.authorize(creds)
        self.sh = gc.open_by_key(sheet_id)
        self.ws_resv = self._ensure_ws(self.sh, ws_resv, HEADERS)
        self.ws_vers = self._ensure_ws(self.sh, ws_vers, VERS_HEADERS)
        self.ws_lock = self._ensure_ws(self.sh, ws_lock, LOCK_HEADERS)

    # ---------- Worksheet helpers ----------
    def _ensure_ws(self, sh, title, headers):
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
        first = ws.row_values(1)
        if first != headers:
            ws.clear()
            ws.append_row(headers)
        return ws

    def _read_all(self, ws) -> List[List[str]]:
        return ws.get_all_values()

    def _find_rows_for_date(self, ws, date_key: str) -> List[int]:
        values = self._read_all(ws)
        rows = []
        for idx, row in enumerate(values, start=1):
            if idx == 1:
                continue
            if len(row) > 0 and row[0] == date_key:
                rows.append(idx)
        return rows

    # ---------- Version (OCC) ----------
    def _get_version(self, date_key: str) -> int:
        vals = self._read_all(self.ws_vers)
        for idx, row in enumerate(vals, start=1):
            if idx == 1:
                continue
            if len(row) >= 2 and row[0] == date_key:
                try:
                    return int(row[1])
                except:
                    return 0
        return 0

    def _set_version(self, date_key: str, new_version: int):
        vals = self._read_all(self.ws_vers)
        for idx, row in enumerate(vals, start=1):
            if idx == 1:
                continue
            if len(row) >= 2 and row[0] == date_key:
                self.ws_vers.update_cell(idx, 2, str(new_version))
                return
        self.ws_vers.append_row([date_key, str(new_version)])

    # ---------- Lock helpers ----------
    def _lock_row_index(self, date_key: str) -> Optional[int]:
        vals = self._read_all(self.ws_lock)
        for idx, row in enumerate(vals, start=1):
            if idx == 1:
                continue
            if len(row) >= 1 and row[0] == date_key:
                return idx
        return None

    def acquire_lock(self, date_key: str, user: str, ttl_sec=30, max_retry=5, backoff=0.5) -> Optional[str]:
        token = str(uuid.uuid4())
        for attempt in range(max_retry):
            row_idx = self._lock_row_index(date_key)
            now = datetime.utcnow()
            expires_at = now + timedelta(seconds=ttl_sec)

            if row_idx is None:
                self.ws_lock.append_row([date_key, token, user, expires_at.isoformat()+"Z"])
            else:
                row = self.ws_lock.row_values(row_idx)
                cur_token = (row[1] if len(row) > 1 else "").strip()
                cur_user  = (row[2] if len(row) > 2 else "").strip()
                cur_exp   = (row[3] if len(row) > 3 else "").strip()
                expired = True
                if cur_exp:
                    try:
                        expired = datetime.fromisoformat(cur_exp.replace("Z","")) <= now
                    except:
                        expired = True
                if cur_token and not expired and cur_token != token:
                    time.sleep(backoff * (attempt+1))
                    continue
                self.ws_lock.update(f"B{row_idx}:D{row_idx}", [[token, user, expires_at.isoformat()+"Z"]])

            # re-check
            row_idx2 = self._lock_row_index(date_key)
            row2 = self.ws_lock.row_values(row_idx2) if row_idx2 else []
            if len(row2) >= 4 and row2[0] == date_key and row2[1] == token:
                return token
            time.sleep(backoff * (attempt+1))
        return None

    def release_lock(self, date_key: str, token: str):
        idx = self._lock_row_index(date_key)
        if not idx:
            return
        row = self.ws_lock.row_values(idx)
        if len(row) >= 2 and row[1] == token:
            self.ws_lock.update(f"B{idx}:D{idx}", [["", "", (datetime.utcnow()-timedelta(seconds=1)).isoformat()+"Z"]])

    # ---------- Partial load/save by date ----------
    def load_date(self, date_key: str) -> Tuple[Dict[str, Any], int]:
        day = {"A": {}, "B": {}}
        # 세션 상태의 블록 목록을 참고(초기화 용도)
        blocks = st.session_state.get("_blocks", [])
        for side in ("A","B"):
            day[side] = {blk["id"]: None for blk in blocks}
        row_idxs = self._find_rows_for_date(self.ws_resv, date_key)
        for r in row_idxs:
            row = self.ws_resv.row_values(r)
            if len(row) >= 6:
                _, court, block_id, user, note, created = row[:6]
                if court in ("A","B"):
                    day[court][block_id] = {"user": user, "note": note, "createdAt": created}
        ver = self._get_version(date_key)
        return day, ver

    def save_date(self, date_key: str, day: Dict[str, Any], expected_version: int, user: str, use_lock=True, ttl_sec=30) -> Tuple[bool, str]:
        token = None
        try:
            if use_lock:
                token = self.acquire_lock(date_key, user=user, ttl_sec=ttl_sec)
                if not token:
                    return False, "LOCK_FAIL"
            current_version = self._get_version(date_key)
            if current_version != expected_version:
                return False, "VERSION_CONFLICT"
            idxs = self._find_rows_for_date(self.ws_resv, date_key)
            for r in sorted(idxs, reverse=True):
                self.ws_resv.delete_rows(r)
            new_rows = []
            for court in ("A","B"):
                for block_id, slot in (day.get(court) or {}).items():
                    if slot:
                        new_rows.append([
                            date_key, court, block_id,
                            slot.get("user",""),
                            slot.get("note",""),
                            slot.get("createdAt","") or datetime.utcnow().isoformat()+"Z",
                        ])
            if new_rows:
                self.ws_resv.append_rows(new_rows, value_input_option="RAW")
            self._set_version(date_key, current_version + 1)
            return True, ""
        finally:
            if use_lock and token:
                self.release_lock(date_key, token)
