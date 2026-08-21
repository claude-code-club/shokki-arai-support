import base64
import calendar
import json
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

import requests
import streamlit as st

from encourage import get_encouragement
from illustrations import get_month_svg

DATA_FILE = Path(__file__).parent / "data" / "records.json"
JST = timezone(timedelta(hours=9))


def today_jst():
    # Streamlit Community Cloud等、サーバーがUTCで動く環境でも
    # 日本時間の「今日」がずれないようにする
    return datetime.now(JST).date()

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


def load_dates():
    if not DATA_FILE.exists():
        return set()
    try:
        return set(json.loads(DATA_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def save_dates(dates):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(sorted(dates), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def calc_current_streak(dates):
    streak = 0
    day = today_jst()
    while day.isoformat() in dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


def calc_best_streak(dates):
    if not dates:
        return 0
    sorted_dates = sorted(date.fromisoformat(d) for d in dates)
    best = 1
    current = 1
    for prev, curr in zip(sorted_dates, sorted_dates[1:]):
        if (curr - prev).days == 1:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


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


def build_month_progress(dates, year, month):
    days_in_month = calendar.monthrange(year, month)[1]
    filled = [
        day for day in range(1, days_in_month + 1)
        if date(year, month, day).isoformat() in dates
    ]
    return days_in_month, filled


st.set_page_config(page_title="食器洗いサポート", page_icon="🍽️")

st.title("🍽️ 食器洗いサポート")
st.caption("洗った日を記録して、毎日の頑張りを見える化しよう")

try:
    weather = fetch_weather()
    st.caption(
        f"今日の東京: {weather['emoji']} {weather['label']}・{weather['temperature']}℃"
    )
except (requests.RequestException, KeyError, ValueError):
    st.caption("（今日の天気は取得できませんでした）")

dates = load_dates()
today_str = today_jst().isoformat()

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
        dates.discard(today_str)
        save_dates(dates)
        st.rerun()
else:
    if st.button("今日、洗いました！"):
        dates.add(today_str)
        save_dates(dates)
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
st.caption("v1.0")
