import streamlit as st
import yfinance as yf
import pandas as pd
import twstock
import hashlib
import json
import os
import uuid
from datetime import datetime
from ta.trend import MACD
from ta.momentum import RSIIndicator

st.set_page_config(page_title="台股選股系統", layout="wide")

# =========================
# 登入設定
# =========================

DEFAULT_LOGIN_PASSWORD = "123456"
ADMIN_TRANSFER_KEY = "Yao!Trade_2026#SafeKey88"

SETTINGS_FILE = "app_settings.json"
OWNER_FILE = "owner_device.key"


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_device_key():
    if not os.path.exists(OWNER_FILE):
        key = str(uuid.uuid4())
        with open(OWNER_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        return key

    with open(OWNER_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        settings = {
            "password_hash": hash_text(DEFAULT_LOGIN_PASSWORD),
            "owner_device_key": get_device_key()
        }
        save_settings(settings)
        return settings

    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


settings = load_settings()
current_device_key = get_device_key()


def require_login():
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    if st.session_state.logged_in:
        return

    st.title("🔐 台股選股系統登入")
    st.info("第一次登入密碼預設為：123456。登入後請到左側管理區修改密碼。")

    password = st.text_input("請輸入登入密碼", type="password")

    if st.button("登入"):
        if hash_text(password) == settings["password_hash"]:
            st.session_state.logged_in = True
            st.rerun()
        else:
            st.error("密碼錯誤")

    st.stop()


require_login()


# =========================
# 股票清單
# =========================

@st.cache_data
def get_all_tw_stocks():
    stocks = {}

    for code, info in twstock.codes.items():
        try:
            if not code.isdigit():
                continue

            if len(code) != 4:
                continue

            if info.type != "股票":
                continue

            if info.market == "上市":
                symbol = f"{code}.TW"
            elif info.market == "上櫃":
                symbol = f"{code}.TWO"
            else:
                continue

            stocks[symbol] = {
                "code": code,
                "name": info.name,
                "market": info.market
            }

        except Exception:
            continue

    return stocks


# =========================
# 資料下載
# =========================

@st.cache_data(show_spinner=False)
def download_price_data(symbol):
    df = yf.download(symbol, period="5y", progress=False, auto_adjust=False)

    if df.empty or len(df) < 250:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    df["VOL20"] = df["Volume"].rolling(20).mean()
    df["HIGH20"] = df["Close"].rolling(20).max()

    macd = MACD(close=df["Close"])
    df["MACD_HIST"] = macd.macd_diff()

    rsi = RSIIndicator(close=df["Close"])
    df["RSI"] = rsi.rsi()

    df["距20日線%"] = (df["Close"] - df["MA20"]) / df["MA20"] * 100
    df["20日波動%"] = df["Close"].rolling(20).std() / df["Close"] * 100

    return df.dropna()


@st.cache_data(show_spinner=False)
def get_recent_trading_value(symbol):
    df = yf.download(symbol, period="5d", progress=False, auto_adjust=False)

    if df.empty:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    latest = df.iloc[-1]
    price = float(latest["Close"])
    volume = float(latest["Volume"])

    return price * volume


# =========================
# 流動性
# =========================

def liquidity_pass(price, volume):
    trading_value = price * volume
    trading_value_million = trading_value / 10_000_000

    if price < 10:
        return False, trading_value_million, "股價低於10元"

    if trading_value < 50_000_000:
        return False, trading_value_million, "日成交金額低於5000萬"

    if trading_value >= 300_000_000:
        return True, trading_value_million, "流動性優質，3億以上"

    if trading_value >= 100_000_000:
        return True, trading_value_million, "流動性良好，1億以上"

    return True, trading_value_million, "流動性合格，5000萬以上"


# =========================
# 技術訊號
# =========================

def is_signal(df, i, strategy_mode):
    today = df.iloc[i]
    yesterday = df.iloc[i - 1]

    if "短線" in strategy_mode:
        return (
            today["Close"] > today["MA20"] and
            today["MA5"] > yesterday["MA5"] and
            today["Volume"] > today["VOL20"] * 1.5 and
            today["MACD_HIST"] > 0 and
            today["Close"] >= today["HIGH20"] and
            today["RSI"] < 75 and
            today["距20日線%"] < 15 and
            today["20日波動%"] < 10
        )

    return (
        today["Close"] > today["MA20"] and
        today["Close"] > today["MA60"] and
        today["MA20"] > today["MA60"] and
        today["MA20"] > yesterday["MA20"] and
        today["Volume"] > today["VOL20"] and
        today["MACD_HIST"] > 0 and
        45 <= today["RSI"] < 70 and
        today["距20日線%"] < 10 and
        today["20日波動%"] < 8
    )


# =========================
# 回測
# =========================

def backtest(df, years=3, strategy_mode="短線（強勢突破）"):
    test_df = df.tail(years * 250).copy()
    trades = []

    for i in range(60, len(test_df) - 20):
        if is_signal(test_df, i, strategy_mode):
            entry = float(test_df.iloc[i]["Close"])
            stop_loss = entry * 0.93
            take_profit = entry * 1.10

            for j in range(i + 1, min(i + 21, len(test_df))):
                low = float(test_df.iloc[j]["Low"])
                high = float(test_df.iloc[j]["High"])
                close = float(test_df.iloc[j]["Close"])

                if low <= stop_loss:
                    trades.append((stop_loss - entry) / entry * 100)
                    break

                if high >= take_profit:
                    trades.append((take_profit - entry) / entry * 100)
                    break

                if j == min(i + 20, len(test_df) - 1):
                    trades.append((close - entry) / entry * 100)
                    break

    if not trades:
        return {
            "交易次數": 0,
            "勝率": 0,
            "平均報酬": 0,
            "最大回撤": 0,
            "賺賠比": 0,
        }

    wins = [x for x in trades if x > 0]
    losses = [x for x in trades if x <= 0]

    win_rate = len(wins) / len(trades) * 100
    avg_return = sum(trades) / len(trades)

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    rr_ratio = avg_win / avg_loss if avg_loss else 0

    equity = 100
    peak = 100
    max_drawdown = 0

    for r in trades:
        equity *= (1 + r / 100)
        peak = max(peak, equity)
        drawdown = (equity - peak) / peak * 100
        max_drawdown = min(max_drawdown, drawdown)

    return {
        "交易次數": len(trades),
        "勝率": round(win_rate, 2),
        "平均報酬": round(avg_return, 2),
        "最大回撤": round(max_drawdown, 2),
        "賺賠比": round(rr_ratio, 2),
    }


# =========================
# 財報分數
# =========================

@st.cache_data(show_spinner=False)
def get_financial_score(symbol):
    score = 0
    notes = []

    try:
        info = yf.Ticker(symbol).info

        eps = info.get("trailingEps", None)
        gross_margin = info.get("grossMargins", None)
        debt_to_equity = info.get("debtToEquity", None)
        revenue_growth = info.get("revenueGrowth", None)

        if eps is not None and eps > 0:
            score += 20
            notes.append("EPS為正")
        else:
            notes.append("EPS不足")

        if gross_margin is not None and gross_margin > 0.2:
            score += 20
            notes.append("毛利率佳")
        else:
            notes.append("毛利率不足")

        if debt_to_equity is not None and debt_to_equity < 150:
            score += 20
            notes.append("負債可接受")
        else:
            notes.append("負債偏高或不足")

        if revenue_growth is not None and revenue_growth > 0:
            score += 20
            notes.append("營收成長")
        else:
            notes.append("營收成長不足")

    except Exception:
        notes.append("財報讀取失敗")

    return score, "、".join(notes)


# =========================
# 分析股票
# =========================

def analyze_stock(symbol, info, strategy_mode):
    df = download_price_data(symbol)

    if df is None or len(df) < 250:
        return None

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest["Close"])
    volume = float(latest["Volume"])

    liquidity_ok, trading_value_million, liquidity_note = liquidity_pass(close, volume)

    tech_score = 0
    conditions = []
    missing = []

    if "短線" in strategy_mode:
        checks = [
            ("站上20日線", close > float(latest["MA20"])),
            ("5日線上彎", float(latest["MA5"]) > float(prev["MA5"])),
            ("量能放大1.5倍", volume > float(latest["VOL20"]) * 1.5),
            ("MACD轉正", float(latest["MACD_HIST"]) > 0),
            ("突破20日高點", close >= float(latest["HIGH20"])),
            ("RSI未過熱", float(latest["RSI"]) < 75),
            ("未過度遠離20日線", float(latest["距20日線%"]) < 15),
            ("波動未過高", float(latest["20日波動%"]) < 10),
        ]
        min_score = 5
    else:
        checks = [
            ("站上20日線", close > float(latest["MA20"])),
            ("站上60日線", close > float(latest["MA60"])),
            ("20日線高於60日線", float(latest["MA20"]) > float(latest["MA60"])),
            ("20日線上彎", float(latest["MA20"]) > float(prev["MA20"])),
            ("量能高於均量", volume > float(latest["VOL20"])),
            ("MACD為正", float(latest["MACD_HIST"]) > 0),
            ("RSI健康區間", 45 <= float(latest["RSI"]) < 70),
            ("波動偏低", float(latest["20日波動%"]) < 8),
        ]
        min_score = 5

    for name, passed in checks:
        if passed:
            tech_score += 1
            conditions.append(name)
        else:
            missing.append(name)

    is_match = tech_score >= min_score and liquidity_ok

    bt3 = backtest(df, years=3, strategy_mode=strategy_mode)
    bt5 = backtest(df, years=5, strategy_mode=strategy_mode)

    financial_score, financial_note = get_financial_score(symbol)

    recent_low = float(df["Low"].tail(20).min())
    ma20 = float(latest["MA20"])

    stop_loss = max(close * 0.93, ma20 * 0.98, recent_low * 0.98)
    tp1 = close * 1.10
    tp2 = close * 1.20

    return {
        "策略模式": strategy_mode,
        "股票代號": info["code"],
        "股票名稱": info["name"],
        "市場": info["market"],
        "收盤價": round(close, 2),

        "是否符合策略": "是" if is_match else "否",
        "差幾條件達標": max(min_score - tech_score, 0),
        "日成交金額_千萬": round(trading_value_million, 2),
        "流動性": liquidity_note,

        "技術分數": tech_score,
        "符合條件": "、".join(conditions) if conditions else "無",
        "未符合條件": "、".join(missing) if missing else "無",

        "距20日線%": round(float(latest["距20日線%"]), 2),
        "20日波動%": round(float(latest["20日波動%"]), 2),
        "RSI": round(float(latest["RSI"]), 2),

        "3年交易次數": bt3["交易次數"],
        "3年勝率%": bt3["勝率"],
        "3年平均報酬%": bt3["平均報酬"],
        "3年最大回撤%": bt3["最大回撤"],
        "3年賺賠比": bt3["賺賠比"],

        "5年交易次數": bt5["交易次數"],
        "5年勝率%": bt5["勝率"],
        "5年平均報酬%": bt5["平均報酬"],
        "5年最大回撤%": bt5["最大回撤"],
        "5年賺賠比": bt5["賺賠比"],

        "財報分數": financial_score,
        "財報備註": financial_note,

        "建議停損": round(stop_loss, 2),
        "第一停利": round(tp1, 2),
        "第二停利": round(tp2, 2),
    }


# =========================
# 評分
# =========================

def calc_total_score(row):
    score = 0

    score += row["技術分數"] * 8
    score += row["財報分數"] * 0.5

    if row["是否符合策略"] == "是":
        score += 15

    if row["3年勝率%"] >= 70:
        score += 25
    elif row["3年勝率%"] >= 60:
        score += 18
    elif row["3年勝率%"] >= 50:
        score += 8

    if row["3年賺賠比"] >= 2:
        score += 25
    elif row["3年賺賠比"] >= 1.5:
        score += 18
    elif row["3年賺賠比"] >= 1:
        score += 8

    if row["3年平均報酬%"] >= 5:
        score += 20
    elif row["3年平均報酬%"] > 0:
        score += 10

    if row["3年最大回撤%"] >= -10:
        score += 15
    elif row["3年最大回撤%"] >= -20:
        score += 8
    elif row["3年最大回撤%"] < -30:
        score -= 15

    if row["3年交易次數"] < 5:
        score -= 25
    elif row["3年交易次數"] < 10:
        score -= 10

    if row["5年平均報酬%"] <= 0:
        score -= 10

    if row["距20日線%"] > 15:
        score -= 12

    if row["20日波動%"] > 8:
        score -= 10

    if row["日成交金額_千萬"] >= 30:
        score += 15
    elif row["日成交金額_千萬"] >= 10:
        score += 8
    elif row["日成交金額_千萬"] >= 5:
        score += 3

    return round(score, 2)


def get_level(score):
    if score >= 90:
        return "A級"
    elif score >= 75:
        return "B級"
    elif score >= 60:
        return "C級"
    return "D級"


def get_action(row):
    if row["是否符合策略"] == "否":
        if row["差幾條件達標"] <= 1:
            return "接近達標，可列入預備觀察"
        return "尚未符合策略，暫不進場"

    if row["等級"] == "A級":
        return "優先觀察，可小倉試單，嚴守停損"
    if row["等級"] == "B級":
        return "可觀察，等拉回20日線或突破確認"
    if row["等級"] == "C級":
        return "只追蹤，不建議直接追高"
    return "不建議操作"


def get_position(row):
    if row["是否符合策略"] == "否":
        return "不進場"

    if row["等級"] == "A級":
        return "小倉 10%~20%"
    if row["等級"] == "B級":
        return "觀察倉 5%~10%"
    if row["等級"] == "C級":
        return "暫不進場"
    return "不操作"


def get_risk(row):
    risks = []

    if row["是否符合策略"] == "否":
        risks.append("策略未達標")

    if row["3年交易次數"] < 10:
        risks.append("樣本偏少")

    if row["3年最大回撤%"] < -30:
        risks.append("回撤偏大")

    if row["3年勝率%"] < 60:
        risks.append("勝率未達60%")

    if row["3年賺賠比"] < 1.5:
        risks.append("賺賠比未達1.5")

    if row["財報分數"] < 40:
        risks.append("財報資料不足或偏弱")

    if row["距20日線%"] > 15:
        risks.append("離20日線過遠")

    if row["20日波動%"] > 8:
        risks.append("短期波動偏大")

    return "、".join(risks) if risks else "風險可接受"


def enrich_result(df):
    df["總分"] = df.apply(calc_total_score, axis=1)
    df["等級"] = df["總分"].apply(get_level)
    df["操作建議"] = df.apply(get_action, axis=1)
    df["建議倉位"] = df.apply(get_position, axis=1)
    df["風險提醒"] = df.apply(get_risk, axis=1)

    return df.sort_values(
        by=["是否符合策略", "總分", "3年勝率%", "3年賺賠比"],
        ascending=[False, False, False, False]
    )


# =========================
# 自動掃描 24 小時快取
# =========================

@st.cache_data(ttl=86400)
def run_scan(scan_limit, strategy_mode):
    stock_pool = get_all_tw_stocks()
    all_items = list(stock_pool.items())

    temp_list = []

    for symbol, info in all_items:
        try:
            trading_value = get_recent_trading_value(symbol)

            if trading_value is not None and trading_value >= 50_000_000:
                temp_list.append({
                    "symbol": symbol,
                    "info": info,
                    "trading_value": trading_value
                })
        except Exception:
            pass

    temp_list = sorted(temp_list, key=lambda x: x["trading_value"], reverse=True)
    selected = temp_list[:int(scan_limit)]

    results = []

    for item in selected:
        try:
            result = analyze_stock(item["symbol"], item["info"], strategy_mode)
            if result:
                results.append(result)
        except Exception:
            pass

    if not results:
        return pd.DataFrame(), 0, 0

    df = pd.DataFrame(results)
    df = enrich_result(df)

    return df, len(temp_list), len(selected)


# =========================
# UI
# =========================

st.title("📈 台股選股系統（進階版）")
st.warning("本工具只提供候選股與操作參考，不保證獲利，也不是自動下單。")

with st.sidebar:
    st.header("設定")

    scan_limit = st.selectbox(
        "掃描股票數量",
        options=[300, 500, 1000],
        format_func=lambda x: f"{x} 檔（{'快速' if x == 300 else '平衡' if x == 500 else '完整'}）",
        index=0
    )

    strategy_mode = st.selectbox(
        "策略模式",
        options=["短線（強勢突破）", "中線（趨勢穩定）"],
        index=0
    )

    st.divider()

    if st.button("🔄 手動刷新今日資料"):
        st.cache_data.clear()
        st.success("快取已清除，請重新整理頁面。")

    st.info("系統會自動快取 24 小時。建議每日 16:00～17:00 後使用。")

    st.divider()

    st.header("等級說明")
    st.markdown("""
    **🟢 A級**
    - 優先觀察
    - 可小倉試單

    **🟡 B級**
    - 條件不錯
    - 等拉回或突破確認

    **🟠 C級**
    - 追蹤即可
    - 不建議追高

    **🔴 D級**
    - 條件不足
    - 不建議操作
    """)

    st.divider()

    st.header("密碼管理")
    is_owner_device = current_device_key == settings.get("owner_device_key")

    if is_owner_device:
        st.success("目前這台電腦有管理權限")

        old_password = st.text_input("目前登入密碼", type="password")
        new_password = st.text_input("新登入密碼", type="password")
        confirm_password = st.text_input("再次輸入新密碼", type="password")

        if st.button("修改登入密碼"):
            if hash_text(old_password) != settings["password_hash"]:
                st.error("目前登入密碼錯誤")
            elif new_password != confirm_password:
                st.error("兩次新密碼不一致")
            elif len(new_password) < 6:
                st.error("新密碼至少 6 碼")
            else:
                settings["password_hash"] = hash_text(new_password)
                save_settings(settings)
                st.success("登入密碼已修改")
    else:
        st.warning("這台電腦目前沒有管理權限")

        transfer_key = st.text_input("輸入管理者轉移金鑰", type="password")

        if st.button("把管理權限轉移到這台電腦"):
            if transfer_key == ADMIN_TRANSFER_KEY:
                settings["owner_device_key"] = current_device_key
                save_settings(settings)
                st.success("管理權限已轉移到這台電腦，請重新整理")
            else:
                st.error("轉移金鑰錯誤")

    if st.button("登出"):
        st.session_state.logged_in = False
        st.rerun()


st.subheader("🔥 今日 Top 10 推薦")

with st.spinner("正在讀取今日分析結果，第一次會比較久，之後會使用快取加速。"):
    df, liquidity_count, selected_count = run_scan(scan_limit, strategy_mode)

if df.empty:
    st.info("目前沒有符合資料。可切換策略、調整掃描數量，或手動刷新。")
else:
    st.success(f"流動性合格 {liquidity_count} 檔，分析成交金額前 {selected_count} 檔。")

    top10 = df.head(10)
    st.dataframe(top10, use_container_width=True, hide_index=True)

    best = df.iloc[0]

    st.subheader("今日第一優先參考")
    st.write(f"策略：{best['策略模式']}")
    st.write(f"股票：{best['股票代號']} {best['股票名稱']}（{best['市場']}）")
    st.write(f"是否符合策略：{best['是否符合策略']}")
    st.write(f"等級：{best['等級']}，總分：{best['總分']}")
    st.write(f"操作建議：{best['操作建議']}")
    st.write(f"建議倉位：{best['建議倉位']}")
    st.write(f"建議停損：{best['建議停損']}")
    st.write(f"第一停利：{best['第一停利']}")
    st.write(f"第二停利：{best['第二停利']}")
    st.write(f"風險提醒：{best['風險提醒']}")

    with st.expander("查看全部分析結果"):
        st.dataframe(df, use_container_width=True, hide_index=True)


st.divider()
st.subheader("🔍 單一股票分析")

single_code = st.text_input("輸入股票代號，例如：2330、2317、2454")

if st.button("分析單一股票"):
    if single_code.strip() == "":
        st.warning("請先輸入股票代號")
    else:
        code = single_code.strip()
        stock_pool = get_all_tw_stocks()

        symbol_tw = f"{code}.TW"
        symbol_two = f"{code}.TWO"

        if symbol_tw in stock_pool:
            symbol = symbol_tw
            info = stock_pool[symbol_tw]
        elif symbol_two in stock_pool:
            symbol = symbol_two
            info = stock_pool[symbol_two]
        else:
            st.error("找不到這檔股票，請確認代號是否正確")
            st.stop()

        with st.spinner(f"正在分析 {code} {info['name']}..."):
            result = analyze_stock(symbol, info, strategy_mode)

        if result:
            df_single = enrich_result(pd.DataFrame([result]))

            st.success(f"{code} {info['name']} 分析完成")
            st.dataframe(df_single, use_container_width=True, hide_index=True)

            row = df_single.iloc[0]

            st.subheader("個股分析結論")
            st.write(f"股票：{row['股票代號']} {row['股票名稱']}（{row['市場']}）")
            st.write(f"策略：{row['策略模式']}")
            st.write(f"是否符合策略：{row['是否符合策略']}")
            st.write(f"等級：{row['等級']}，總分：{row['總分']}")
            st.write(f"操作建議：{row['操作建議']}")
            st.write(f"建議倉位：{row['建議倉位']}")
            st.write(f"未符合條件：{row['未符合條件']}")
            st.write(f"風險提醒：{row['風險提醒']}")
        else:
            st.warning("資料不足，無法分析這檔股票。")