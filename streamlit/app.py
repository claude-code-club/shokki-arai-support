import base64

import requests
import streamlit as st

from encourage import get_encouragement
from illustrations import get_month_svg
from logic import (
    build_month_progress,
    calc_best_streak,
    calc_current_streak,
    today_jst,
)
import auth
from storage import (
    RecordsFileCorruptedError,
    StorageConfigError,
    StorageUnavailableError,
    add_date,
    cancel_date,
    get_tenant_id,
    load_dates,
    rename_tenant,
)

# 東京の座標。Open-Meteo は無料・APIキー不要の天気API。
WEATHER_API_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_PARAMS = {"latitude": 35.6895, "longitude": 139.6917, "current_weather": True}

WEATHER_CODE_MAP = {
    0: ("☀️", "快晴"),
    1: ("🌤️", "おおむね晴れ"),
    2: ("⛅", "一部くもり"),
    3: ("☁️", "くもり"),
    45: ("🌫️", "霧"),
    48: ("🌫️", "霧"),
    51: ("🌦️", "小雨"),
    53: ("🌦️", "小雨"),
    55: ("🌧️", "雨"),
    61: ("🌧️", "雨"),
    63: ("🌧️", "雨"),
    65: ("🌧️", "強い雨"),
    71: ("🌨️", "雪"),
    73: ("🌨️", "雪"),
    75: ("❄️", "強い雪"),
    80: ("🌦️", "にわか雨"),
    81: ("🌧️", "にわか雨"),
    82: ("⛈️", "激しいにわか雨"),
    95: ("⛈️", "雷雨"),
}


@st.cache_data(ttl=600)
def fetch_weather():
    response = requests.get(WEATHER_API_URL, params=WEATHER_PARAMS, timeout=5)
    response.raise_for_status()
    current = response.json()["current_weather"]
    emoji, label = WEATHER_CODE_MAP.get(current["weathercode"], ("🌡️", "不明"))
    return {"emoji": emoji, "label": label, "temperature": current["temperature"]}


MONTH_THEME = {
    1: ("🎍", "正月・松"),
    2: ("🍫", "節分・バレンタイン"),
    3: ("🎎", "ひな祭り"),
    4: ("🌸", "桜"),
    5: ("🎏", "こどもの日・鯉のぼり"),
    6: ("☔", "梅雨・紫陽花"),
    7: ("🎋", "七夕"),
    8: ("🎆", "花火・夏祭り"),
    9: ("🌕", "お月見"),
    10: ("🍁", "紅葉・ハロウィン"),
    11: ("🍁", "紅葉"),
    12: ("🎄", "クリスマス"),
}


st.set_page_config(page_title="食器洗いサポート", page_icon="🍽️")

st.title("🍽️ 食器洗いサポート")
st.caption("食器洗いを記録して、毎日の頑張りと小さな達成感を見える化しよう")

try:
    weather = fetch_weather()
    st.caption(
        f"今日の東京: {weather['emoji']} {weather['label']}・{weather['temperature']}℃"
    )
except (requests.RequestException, KeyError, ValueError):
    st.caption("（今日の天気は取得できませんでした）")

USER_ROLE = None

if auth.is_auth_enabled():
    try:
        TENANT_ID, USER_ROLE = auth.require_login_and_resolve_tenant()
    except auth.EmailNotVerifiedError:
        st.error("メールアドレスの確認を完了してください。")
        st.stop()
    except auth.AccessDeniedError as e:
        st.error(str(e))
        st.stop()
    if TENANT_ID is None:
        # 未ログイン。require_login_and_resolve_tenant()が既にログイン導線を表示済み。
        st.stop()
    st.sidebar.button("ログアウト", on_click=st.logout)
else:
    TENANT_ID = get_tenant_id()

try:
    dates = load_dates(tenant_id=TENANT_ID)
except RecordsFileCorruptedError:
    st.error(
        "記録データ(records.json)の読み込みに失敗しました。ファイルが壊れている可能性があるため、"
        "安全のため保存・表示処理を停止しました。管理者に連絡してください"
        "(復元手順は仕様書/本番DB運用.mdを参照)。"
    )
    st.stop()
except (StorageConfigError, StorageUnavailableError):
    # 接続情報や内部エラーの詳細は画面に出さず、一般向けの安全なメッセージのみ表示する。
    st.error(
        "記録データの保存先に問題が発生しているため、安全のため表示・保存処理を停止しました。"
        "しばらくしてから再度お試しいただくか、管理者に連絡してください。"
    )
    st.stop()
today_str = today_jst().isoformat()


def add_date_safely(record_date):
    """記録保存時のStorageConfigError/StorageUnavailableErrorを、初回読み込み時と
    同じ一般向けの安全なメッセージで捕捉し、st.stop()する(内部エラーの詳細は画面に出さない)。
    """
    try:
        add_date(record_date, tenant_id=TENANT_ID)
    except (StorageConfigError, StorageUnavailableError):
        st.error(
            "記録の保存中に問題が発生しました。安全のため処理を停止しました。"
            "しばらくしてから再度お試しいただくか、管理者に連絡してください。"
        )
        st.stop()


def cancel_date_safely(record_date):
    """記録取り消し時のStorageConfigError/StorageUnavailableErrorを、add_date_safely()と
    同じ一般向けの安全なメッセージで捕捉し、st.stop()する。
    """
    try:
        cancel_date(record_date, tenant_id=TENANT_ID)
    except (StorageConfigError, StorageUnavailableError):
        st.error(
            "記録の取り消し中に問題が発生しました。安全のため処理を停止しました。"
            "しばらくしてから再度お試しいただくか、管理者に連絡してください。"
        )
        st.stop()


@st.cache_data(ttl=3600)
def cached_encouragement(streak, best_streak, day_key):
    # day_key はキャッシュキーを日替わりにするためだけの引数（Claude APIの呼びすぎ防止）
    return get_encouragement(streak, best_streak)


if today_str in dates:
    st.success("今日はもう記録済みです。えらい！")
    comment = cached_encouragement(
        calc_current_streak(dates), calc_best_streak(dates), today_str
    )
    if comment:
        st.info(comment)
    if st.button("今日の記録を取り消す"):
        cancel_date_safely(today_str)
        st.rerun()
else:
    if st.button("今日、洗いました！"):
        add_date_safely(today_str)
        st.rerun()

st.divider()

col1, col2 = st.columns(2)
col1.metric("現在の連続記録", f"{calc_current_streak(dates)} 日")
col2.metric("最長記録", f"{calc_best_streak(dates)} 日")

st.divider()

today = today_jst()
days_in_month, filled_days = build_month_progress(dates, today.year, today.month)
_, month_label = MONTH_THEME.get(today.month, ("", ""))
st.subheader(f"今月のジグソーパズル（{today.month}月・{month_label}）")
st.progress(len(filled_days) / days_in_month)

CELL_PX = 60
GRID_COLS = 7
GRID_ROWS = 5
svg_b64 = base64.b64encode(get_month_svg(today.month).encode("utf-8")).decode("ascii")
bg_url = f"data:image/svg+xml;base64,{svg_b64}"

cells_html = ""
for day in range(1, days_in_month + 1):
    col = (day - 1) % GRID_COLS
    row = (day - 1) // GRID_COLS
    bg_pos = f"-{col * CELL_PX}px -{row * CELL_PX}px"
    filled = day in filled_days
    piece_style = (
        f"width:{CELL_PX}px;height:{CELL_PX}px;border-radius:6px;"
        f"background-image:url('{bg_url}');"
        f"background-size:{GRID_COLS * CELL_PX}px {GRID_ROWS * CELL_PX}px;"
        f"background-position:{bg_pos};"
        + (
            "box-shadow:0 0 0 2px rgba(255,255,255,0.7) inset;"
            if filled
            else "filter:grayscale(1) brightness(1.7) opacity(0.3);"
        )
    )
    cells_html += (
        f'<div style="position:relative;{piece_style}">'
        f'<span style="position:absolute;bottom:1px;right:3px;font-size:0.55rem;'
        f'color:{"#fff" if filled else "#999"};text-shadow:0 0 2px rgba(0,0,0,0.5);">{day}</span>'
        f"</div>"
    )

st.markdown(
    f'<div style="display:grid;grid-template-columns:repeat({GRID_COLS}, {CELL_PX}px);gap:3px;">{cells_html}</div>',
    unsafe_allow_html=True,
)

if len(filled_days) == days_in_month:
    st.success(f"{today.month}月のパズル完成！今月も皆勤でした🎉")
    st.balloons()
else:
    st.caption(f"{len(filled_days)} / {days_in_month} マス埋まっています")

st.divider()
st.subheader("記録した日")

if not dates:
    st.caption("まだ記録がありません。")
else:
    recent = sorted(dates, reverse=True)[:14]
    st.write("　".join(recent))

st.caption(f"累計記録日数: {len(dates)}日")

if auth.is_auth_enabled() and USER_ROLE == "admin":
    st.divider()
    st.subheader("世帯の設定(admin専用)")
    new_name = st.text_input("世帯名")
    if st.button("世帯名を変更する") and new_name:
        try:
            rename_tenant(new_name, tenant_id=TENANT_ID, role=USER_ROLE)
        except (StorageConfigError, StorageUnavailableError):
            st.error(
                "世帯名の変更中に問題が発生しました。安全のため処理を停止しました。"
                "しばらくしてから再度お試しいただくか、管理者に連絡してください。"
            )
            st.stop()
        st.success("世帯名を変更しました。")

st.caption("v1.1")
