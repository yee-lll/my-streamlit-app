import os, re, io, csv, json, uuid, html, hashlib, sqlite3, smtplib, subprocess, tempfile, base64
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from email.message import EmailMessage

import pandas as pd
import streamlit as st
from streamlit_calendar import calendar
from openai import OpenAI
from pypdf import PdfReader
from docx import Document
from pptx import Presentation

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
)

# 페이지 설정
st.set_page_config(
    page_title="에이블 | AI Work Assistant",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded"
)

KST = ZoneInfo("Asia/Seoul")
TODAY = datetime.now(KST).date()

DATA_DIR = Path(os.getenv("AIVLE_DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "aivle.db"


NAV_ITEMS = [
    "대시보드",
    "AI 비서",
    "문서 검색",
    "회의록 분석",
    "회의록 결과",
    "일정·할 일",
    "이메일 관리",
    "환경설정"
]

ICON = {
    "대시보드": "▦",
    "AI 비서": "✦",
    "문서 검색": "📚",
    "회의록 분석": "▤",
    "일정·할 일": "▣"
}


FILE_TYPES = [
    "txt", "pdf", "m4a", "mp3", "mp4", "wav",
    "docx", "csv", "xlsx", "xls", "pptx", "ppt"
]

# API 설정
def load_settings():
    data = dict(os.environ)

    for f in [DATA_DIR / "api_key.txt", Path("api_key.txt")]:
        if f.exists():
            for line in f.read_text(
                encoding="utf-8-sig"
            ).splitlines():

                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = (
                        v.strip().strip('"').strip("'")
                    )

                elif line.startswith("sk-"):
                    data["OPENAI_API_KEY"] = line

            break

    try:
        for k in (
            "OPENAI_API_KEY",
            "GMAIL_ADDRESS",
            "GMAIL_APP_PASSWORD",
            "MAIL_TO"
        ):
            if k in st.secrets:
                data[k] = st.secrets[k]
    except Exception:
        pass

    return data


SETTINGS = load_settings()

if not SETTINGS.get("OPENAI_API_KEY"):
    st.error(
        "api_key.txt 또는 Streamlit Secrets에 "
        "OPENAI_API_KEY가 없습니다."
    )
    st.stop()

client = OpenAI(
    api_key=SETTINGS["OPENAI_API_KEY"],
    timeout=90,
    max_retries=1
)

# 세션 상태
DEFAULT_STATE = {
    "page": "대시보드",
    "user": "김대리",
    "loaded_user": None,
    "messages": [],
    "meeting_result": None,
    "meeting_text": "",
    "uploaded_docs": [],
    "upload_digest": "",
    "meeting_digest": "",
    "pending_email": None,
    "last_sent_token": "",
    "voice_stage": "idle",
    "reply_audio": "",
    "reply_id": "",
    "last_event": "",
    "voice_mode": "음성 + 텍스트",
    "voice_name": "nova",
    "voice_speed": 1.0,
    "hide_welcome": False,
    "auto_save": True,
    "sidebar_order": NAV_ITEMS.copy(),
    "sidebar_pins": [],
    "summary_top": True,
    "last_recording": "",
    "mail_notice": "",
    "selected_task": None,
    "pending_doc_request": None,
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

S = st.session_state


def kst_today():
    return datetime.now(KST).date()


def valid_email(x):
    return bool(re.fullmatch(
        r"[^\s@]+@[^\s@]+\.[^\s@]+",
        str(x).strip()
    ))


def normal(x):
    return re.sub(
        r"[\s,.!?~]", "", str(x or "")
    ).lower()


def fmt_date(x):
    if not x:
        return None
    try:
        return date.fromisoformat(str(x)[:10]).isoformat()
    except ValueError:
        return None


def navigate(page):
    S.page = page
    st.rerun()

# === ABLE LOGIN START ===
import hmac

# 아이디 저장용 브라우저 저장소
LOGIN_STORAGE = st.components.v2.component(
    name="able_login_id_storage_2026",
    js="""
    export default function({ data, setStateValue }) {
        const key = "able_remembered_login_id";
        try {
            if (data.action === "save") {
                localStorage.setItem(key, data.value || "");
            } else if (data.action === "remove") {
                localStorage.removeItem(key);
            } else {
                setStateValue("saved_id", localStorage.getItem(key) || "");
            }
        } catch(e) {
            if (data.action === "read") {
                setStateValue("saved_id", "");
            }
        }
    }
    """
)

# 로그인 계정 정보 가져오기
def login_setting(name):
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass

    return str(SETTINGS.get(name, "")).strip()


# 대소문자를 구분하여 안전하게 비교
def same_text(a, b):
    return hmac.compare_digest(
        str(a).encode("utf-8"),
        str(b).encode("utf-8")
    )


# 로그인 화면
def render_login():
    st.markdown("""
        <style>
        [data-testid="stSidebar"],
        [data-testid="stSidebarCollapsedControl"] {
            display: none;
        }
        </style>
    """, unsafe_allow_html=True)

    # 브라우저에 저장된 아이디 읽기
    remembered = LOGIN_STORAGE(
        data={"action": "read"},
        default={"saved_id": None},
        on_saved_id_change=lambda: None,
        key="able_login_storage_read"
    ).saved_id

    if remembered is not None and not S.get("_login_id_loaded", False):
        S._login_id_loaded = True

        if remembered:
            S["able_login_id"] = remembered
            S["able_remember_id"] = True

    _, center, _ = st.columns([1, 1.2, 1])

    with center:
        st.markdown("## ✦ 에이블")
        st.caption("AI Work Assistant · 로그인")
        st.divider()

        # 로그인 방식 선택
        method = st.radio(
            "로그인 방식",
            ["아이디", "휴대폰", "사원번호"],
            horizontal=True,
            key="able_login_method"
        )

        field_key = {
            "아이디": "able_login_id",
            "휴대폰": "able_login_phone",
            "사원번호": "able_login_employee_no"
        }[method]

        # 로그인 입력 폼
        with st.form("able_login_form"):
            identifier = st.text_input(
                method,
                key=field_key
            )

            password = st.text_input(
                "비밀번호",
                type="password"
            )

            remember_id = st.checkbox(
                "아이디 저장",
                key="able_remember_id"
            )

            submitted = st.form_submit_button(
                "로그인",
                type="primary",
                use_container_width=True
            )

        if submitted:
            expected_id = login_setting("APP_LOGIN_ID")
            expected_pw = login_setting("APP_LOGIN_PASSWORD")
            expected_phone = login_setting("APP_LOGIN_PHONE")
            expected_no = login_setting("APP_LOGIN_EMPLOYEE_NO")

            if not expected_id or not expected_pw:
                st.error(
                    "로그인 계정 정보가 설정되지 않았습니다. "
                    "api_key.txt 또는 Secrets를 확인하세요."
                )
                return

            # 로그인 방식별 계정 확인
            if method == "아이디":
                matched = same_text(
                    identifier.strip(), expected_id
                )

            elif method == "휴대폰":
                matched = bool(expected_phone) and same_text(
                    re.sub(r"\D", "", identifier),
                    re.sub(r"\D", "", expected_phone)
                )

            else:
                matched = bool(expected_no) and same_text(
                    identifier.strip(), expected_no
                )

            # 아이디 및 비밀번호 검증
            if matched and same_text(password, expected_pw):
                S.authenticated = True
                S.login_id = expected_id
                S.loaded_user = None

                S._remember_action = {
                    "action": "save" if remember_id else "remove",
                    "value": expected_id if remember_id else ""
                }

                st.rerun()

            else:
                st.error("로그인 정보가 일치하지 않습니다.")


# 로그인하지 않은 경우 앱 사용 차단
if not S.get("authenticated", False):
    render_login()
    st.stop()


# 로그인 성공 후 아이디 저장 또는 삭제
if S.get("_remember_action"):
    LOGIN_STORAGE(
        data=S._remember_action,
        key="able_login_storage_write"
    )

    S._remember_action = None

# === ABLE LOGIN END ===

