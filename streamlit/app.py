import base64
import os

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
import billing
import metering
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
    if st.user.is_logged_in:
        # ログイン済みなら、世帯所属の解決結果によらず常にログアウトできるようにする
        # (確認待ち・未所属の画面で行き詰まらないようにするため)。
        st.sidebar.button("ログアウト", on_click=st.logout)
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
else:
    TENANT_ID = get_tenant_id()


def render_standard_upgrade_cta(key_suffix):
    """第18回のCheckout Session作成をそのまま再利用する、Standardアップグレード導線
    (第20回: プラン制限とメータリング)。admin専用。呼び出し箇所ごとに一意なkey_suffixを
    渡すこと(同じ画面に複数のボタンを置けるようにするため)。
    """
    if st.button("Standardへアップグレードする（テスト決済）", key=f"_upgrade_button_{key_suffix}"):
        base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
        try:
            checkout_session = billing.start_checkout_session(
                tenant_id=TENANT_ID,
                role=USER_ROLE,
                success_url=f"{base_url}/?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{base_url}/?billing=cancelled",
            )
            st.session_state["_checkout_url"] = checkout_session.url
        except billing.PermissionDeniedError:
            st.error("この操作にはadmin権限が必要です。")
        except billing.BillingConfigError:
            st.error("課金機能が正しく設定されていません。管理者に連絡してください。")
        except billing.StripeApiError:
            st.error("Stripeとの通信に失敗しました。しばらくしてから再度お試しください。")

    checkout_url = st.session_state.get("_checkout_url")
    if checkout_url:
        st.link_button("お支払いへ進む（Stripeのページへ移動します）", checkout_url)


def build_30day_analysis(dates):
    """直近30日間の記録日数を集計する(第20回: プラン制限とメータリング、Standard限定)。

    純粋関数(DB・Streamlitに依存しない)。datesは既にDBから読み込み済みの集合を渡す。
    """
    from datetime import timedelta

    today = today_jst()
    window = [(today - timedelta(days=i)).isoformat() for i in range(30)]
    filled_in_window = sorted((d for d in window if d in dates), reverse=True)
    return {"filled_count": len(filled_in_window), "total_days": 30, "filled_dates": filled_in_window}


def generate_monthly_reflection(filled_days, days_in_month, month_label):
    """「今月の振り返り」の本文を組み立てる(第20回: プラン制限とメータリング)。

    純粋関数。外部API(Claude等)は使わず、既存の集計値だけから生成する(最小構成)。
    """
    filled_count = len(filled_days)
    rate = filled_count / days_in_month
    if rate >= 1.0:
        return f"今月は{month_label}、皆勤達成です！素晴らしい継続力ですね。"
    if rate >= 0.7:
        return f"今月は{month_label}、{filled_count}/{days_in_month}日達成。とても良いペースです。"
    if rate >= 0.3:
        return f"今月は{month_label}、{filled_count}/{days_in_month}日達成。無理のないペースで続けましょう。"
    return f"今月は{month_label}、{filled_count}/{days_in_month}日。焦らず、できる日から積み重ねましょう。"


if auth.is_auth_enabled() and billing.is_billing_enabled():
    st.divider()
    st.subheader("プラン")

    query_params = st.query_params
    if "session_id" in query_params:
        session_id = query_params.get("session_id")
        try:
            billing.apply_checkout_session(
                session_id=session_id, tenant_id=TENANT_ID, role=USER_ROLE
            )
            st.success("お支払いが完了し、Standardプランへ変更されました。")
        except billing.PermissionDeniedError:
            st.error("この操作にはadmin権限が必要です。")
        except (billing.InvalidSessionError, billing.TenantMismatchError):
            st.error("お支払い状況を確認できませんでした。お手数ですが再度お試しください。")
        except (billing.StripeApiError, billing.BillingUnavailableError, billing.BillingConfigError):
            st.error(
                "お支払い状況の確認中に問題が発生しました。"
                "しばらくしてから再度お試しいただくか、管理者に連絡してください。"
            )
        st.query_params.clear()
    elif query_params.get("billing") == "cancelled":
        st.info("お支払いをキャンセルしました。プランはFreeのままです。")
        st.query_params.clear()

    try:
        plan_status = billing.fetch_plan_status(TENANT_ID)
    except (billing.BillingUnavailableError, billing.BillingConfigError):
        plan_status = None
        st.caption("プラン情報を取得できませんでした。")

    if plan_status is not None:
        is_standard = plan_status["plan"] == "standard"
        st.write(f"現在のプラン: **{'Standard（月額500円）' if is_standard else 'Free'}**")
        st.caption("Free：0円 ／ Standard：月額500円（テストモードでの仮価格）")

        if not is_standard and USER_ROLE == "admin":
            render_standard_upgrade_cta("plan_section")

        if is_standard and USER_ROLE == "admin":
            if st.button("サブスクを管理する（解約はこちらから）"):
                base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
                try:
                    portal_session = billing.start_billing_portal_session(
                        tenant_id=TENANT_ID, role=USER_ROLE, return_url=f"{base_url}/"
                    )
                    st.session_state["_billing_portal_url"] = portal_session.url
                except billing.PermissionDeniedError:
                    st.error("この操作にはadmin権限が必要です。")
                except billing.NoActiveSubscriptionError:
                    st.error("有効な契約が見つかりませんでした。")
                except billing.BillingConfigError:
                    st.error("課金機能が正しく設定されていません。管理者に連絡してください。")
                except billing.StripeApiError:
                    st.error("Stripeとの通信に失敗しました。しばらくしてから再度お試しください。")

            portal_url = st.session_state.get("_billing_portal_url")
            if portal_url:
                st.link_button("サブスクの管理へ進む（Stripeのページへ移動します）", portal_url)

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

if auth.is_auth_enabled() and billing.is_billing_enabled():
    # --- 30日間の詳細分析(第20回: プラン制限とメータリング、Standard限定) ---
    st.divider()
    st.subheader("30日間の詳細分析（Standard限定）")
    try:
        plan_status_for_analysis = billing.fetch_plan_status(TENANT_ID)
        standard_access = billing.has_standard_access(plan_status_for_analysis)
    except (billing.BillingUnavailableError, billing.BillingConfigError):
        # 契約状態を確認できない場合は開放しない(fail closed。仕様書/プラン制限・メータリング設計.md⑨参照)
        standard_access = False

    if standard_access:
        analysis = build_30day_analysis(dates)
        st.metric("直近30日間の記録日数", f"{analysis['filled_count']} / {analysis['total_days']} 日")
        if analysis["filled_dates"]:
            st.write("　".join(analysis["filled_dates"]))
        else:
            st.caption("直近30日間の記録はまだありません。")
    else:
        st.info("🔒 この機能はStandardプラン限定です。")
        if USER_ROLE == "admin":
            render_standard_upgrade_cta("analysis_gate")

    # --- 今月の振り返り(第20回: プラン制限とメータリング、Free月3回まで) ---
    st.divider()
    st.subheader("今月の振り返り")
    try:
        reflection_status = metering.fetch_monthly_reflection_status(TENANT_ID)
    except metering.MeteringUnavailableError:
        reflection_status = None
        st.caption("利用状況を取得できませんでした。しばらくしてから再度お試しください。")

    if reflection_status is not None:
        if reflection_status["has_standard_access"]:
            st.caption("Standard: 今月の振り返りは無制限で利用できます。")
        else:
            st.caption(
                f"Free: 今月の利用回数 {reflection_status['usage_count']} / "
                f"{reflection_status['limit']} 回"
            )

        if st.button("今月の振り返りを見る"):
            try:
                metering.use_monthly_reflection(TENANT_ID)
                st.session_state["_reflection_limit_reached"] = False
                st.session_state["_reflection_text"] = generate_monthly_reflection(
                    filled_days, days_in_month, month_label
                )
                st.rerun()
            except metering.UsageLimitExceededError:
                st.session_state["_reflection_text"] = None
                st.session_state["_reflection_limit_reached"] = True
            except metering.MeteringUnavailableError:
                st.error(
                    "利用状況の更新に失敗しました。安全のため処理を停止しました。"
                    "しばらくしてから再度お試しいただくか、管理者に連絡してください。"
                )
                st.stop()

        if st.session_state.get("_reflection_limit_reached"):
            st.warning(
                f"今月の無料利用回数（{reflection_status['limit']}回）を使い切りました。"
                "Standardへアップグレードすると無制限で利用できます。"
            )
            if USER_ROLE == "admin":
                render_standard_upgrade_cta("reflection_limit")

        if st.session_state.get("_reflection_text"):
            st.info(st.session_state["_reflection_text"])

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
