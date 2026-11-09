# -*- coding: utf-8 -*-
"""PChome × MOMO 比價系統 (Streamlit)

功能：
  * 輸入關鍵字與爬取頁數，同時查詢 PChome 與 MOMO（兩平台並行爬取）
  * 單平台統計 / 兩平台比價總覽
  * 圖表：箱型圖、直方圖、折線圖、圓餅圖、旭日圖
  * 相似商品自動配對比價（名稱模糊比對 + 規格檢查）
  * 篩選（價格範圍、排除關鍵字）與 CSV 下載

執行方式：
    pip install -r requirements.txt
    streamlit run app.py
"""
from __future__ import annotations

import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from curl_cffi import requests as cffi
from plotly.subplots import make_subplots

# ======================================================================
# 常數
# ======================================================================
PCHOME_API = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
PCHOME_ITEM_URL = "https://24h.pchome.com.tw/prod/{pid}"
MOMO_API = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MOMO_HOME = "https://www.momoshop.com.tw/"
MOMO_ITEM_URL = "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code={code}"

PLATFORMS = ["PChome", "MOMO"]
COLORS = {"PChome": "#2E86DE", "MOMO": "#E84393", "同價": "#95A5A6"}
COLUMNS = ["平台", "頁數", "品名", "價格", "原價", "連結"]

PCHOME_SORTS = {"銷量高→低": "sale/dc", "價格低→高": "prc/ac", "價格高→低": "prc/dc"}

PCHOME_HEADERS = {"Accept": "application/json", "Accept-Language": "zh-TW,zh;q=0.9"}
MOMO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}

# Streamlit 新版以 width="stretch" 取代 use_container_width，這裡自動相容
_VER = tuple(int(x) for x in re.findall(r"\d+", st.__version__)[:2])
STRETCH = {"width": "stretch"} if _VER >= (1, 50) else {"use_container_width": True}

st.set_page_config(page_title="PChome × MOMO 比價", page_icon="🛒", layout="wide")


# ======================================================================
# 爬蟲
# ======================================================================
_NUM = re.compile(r"\d[\d,]*")


def to_int(value) -> int | None:
    """取出字串中『第一個』數字（'$1,299'→1299；'399~599'→399），失敗回傳 None。"""
    if value is None:
        return None
    m = _NUM.search(str(value))
    return int(m.group().replace(",", "")) if m else None


def http_request(session, method: str, url: str, retries: int = 2, **kwargs):
    """帶重試的請求：只對 429 / 5xx / 連線例外重試，其餘錯誤直接丟出。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.request(method, url, timeout=20, **kwargs)
            if resp.status_code == 200:
                return resp
            last = RuntimeError(f"HTTP {resp.status_code}：{resp.text[:120]}")
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def crawl_pchome(keyword: str, pages: int, sort: str, delay: float):
    rows, errors = [], []
    with cffi.Session(impersonate="chrome") as s:
        for page in range(1, pages + 1):
            try:
                resp = http_request(
                    s, "GET", PCHOME_API, headers=PCHOME_HEADERS,
                    params={"q": keyword, "page": page, "sort": sort},
                )
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                errors.append(f"PChome 第 {page} 頁失敗：{e}")
                break

            prods = data.get("prods") or []
            if not prods:
                break
            for p in prods:
                pid = p.get("Id", "")
                rows.append({
                    "平台": "PChome",
                    "頁數": page,
                    "品名": p.get("name", ""),
                    "價格": to_int(p.get("price")),
                    "原價": to_int(p.get("originPrice")),
                    "連結": PCHOME_ITEM_URL.format(pid=pid) if pid else "",
                })
            total = data.get("totalPage")
            if isinstance(total, int) and page >= total:
                break
            if page < pages:
                time.sleep(delay)
    return rows, errors


def momo_payload(keyword: str, page: int) -> dict:
    return {
        "host": "momoshop",
        "flag": "searchEngine",
        "data": {
            "searchValue": keyword,
            "searchType": "1",
            "cateLevel": "-1",
            "curPage": str(page),
            "priceS": "0",
            "priceE": "9999999",
            "showType": "chessboardType",
            "isFuzzy": "0",
            "rtnCateDatainfo": {
                "cateCode": "",
                "cateLv": "-1",
                "keyword": keyword,
                "curPage": str(page),
                "historyDoPush": "false",
                "timestamp": int(time.time() * 1000),
            },
            "flag": 2018,
            "serviceCode": "MT01",
            "addressSearchData": {},
        },
    }


def crawl_momo(keyword: str, pages: int, delay: float):
    rows, errors = [], []
    with cffi.Session(impersonate="chrome") as s:  # 模擬 Chrome TLS 指紋
        try:
            s.get(MOMO_HOME, headers=MOMO_HEADERS, timeout=15)  # 先取得 cookie
        except Exception as e:  # noqa: BLE001
            errors.append(f"MOMO 取得首頁 cookie 失敗：{e}")

        for page in range(1, pages + 1):
            try:
                resp = http_request(
                    s, "POST", MOMO_API, headers=MOMO_HEADERS,
                    json=momo_payload(keyword, page),
                )
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                errors.append(f"MOMO 第 {page} 頁失敗：{e}")
                break

            goods = (data.get("rtnSearchData") or {}).get("goodsInfoList") or []
            if not goods:
                break
            for g in goods:
                code = g.get("goodsCode", "")
                rows.append({
                    "平台": "MOMO",
                    "頁數": page,
                    "品名": g.get("goodsName", ""),
                    "價格": to_int(g.get("goodsPrice")),
                    "原價": None,
                    "連結": MOMO_ITEM_URL.format(code=code) if code else "",
                })
            if page < pages:
                time.sleep(delay)
    return rows, errors


@st.cache_data(ttl=600, show_spinner=False)
def crawl_all(keyword: str, pages: int, platforms: tuple, pchome_sort: str, delay: float):
    """並行爬取所選平台，回傳 (統一格式 DataFrame, 錯誤訊息清單)。結果快取 10 分鐘。"""
    jobs = {
        "PChome": lambda: crawl_pchome(keyword, pages, pchome_sort, delay),
        "MOMO": lambda: crawl_momo(keyword, pages, delay),
    }
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, len(platforms))) as ex:
        futures = {name: ex.submit(jobs[name]) for name in platforms}
        for name, fut in futures.items():
            try:
                r, e = fut.result()
                rows += r
                errors += e
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name} 發生未預期錯誤：{e}")

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["價格"] = pd.to_numeric(df["價格"], errors="coerce")
    df["原價"] = pd.to_numeric(df["原價"], errors="coerce")
    df = df.dropna(subset=["價格"])
    df = df[df["價格"] > 0].copy()
    df["價格"] = df["價格"].astype(int)
    df["原價"] = df["原價"].where(df["原價"] > df["價格"])  # 原價沒高於售價就視為無折扣
    df = df.drop_duplicates(subset=["平台", "品名", "價格"]).reset_index(drop=True)
    return df, errors


# ======================================================================
# 相似商品配對
# ======================================================================
def _tokens(name: str) -> tuple[set, set]:
    """回傳 (全部 token, 規格 token)。中文取 bigram，英數字取連續片段。"""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)          # 10,000 → 10000
    s = re.sub(r"(\d)\s+([a-z])", r"\1\2", s)         # 10000 mAh → 10000mah
    ascii_tokens = {t for t in re.findall(r"[a-z0-9]+(?:\.\d+)?", s) if len(t) >= 2}
    cjk = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        cjk.update(run[i:i + 2] for i in range(len(run) - 1))
    spec = {t for t in ascii_tokens if re.search(r"\d", t) and re.search(r"[a-z]", t)}
    return ascii_tokens | cjk, spec


@st.cache_data(show_spinner=False)
def match_products(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """為每個 PChome 商品找最相似的 MOMO 商品（Dice 係數）。
    若雙方都有規格（如 10000mah、20w）卻完全不重疊，視為不同商品。"""
    a = df[df["平台"] == "PChome"].reset_index(drop=True)
    b = df[df["平台"] == "MOMO"].reset_index(drop=True)
    if a.empty or b.empty:
        return pd.DataFrame()

    ta = [_tokens(n) for n in a["品名"]]
    tb = [_tokens(n) for n in b["品名"]]

    out = []
    for i, (toks_a, spec_a) in enumerate(ta):
        best, best_j = 0.0, -1
        for j, (toks_b, spec_b) in enumerate(tb):
            if spec_a and spec_b and not (spec_a & spec_b):
                continue
            inter = len(toks_a & toks_b)
            if not inter:
                continue
            score = 2 * inter / (len(toks_a) + len(toks_b))
            if score > best:
                best, best_j = score, j
        if best_j >= 0 and best >= threshold:
            pa, pb = a.loc[i], b.loc[best_j]
            diff = int(pb["價格"] - pa["價格"])
            out.append({
                "PChome品名": pa["品名"], "PChome價格": int(pa["價格"]), "PChome連結": pa["連結"],
                "MOMO品名": pb["品名"], "MOMO價格": int(pb["價格"]), "MOMO連結": pb["連結"],
                "價差(MOMO-PChome)": diff,
                "較便宜": "PChome" if diff > 0 else ("MOMO" if diff < 0 else "同價"),
                "相似度": round(best, 2),
            })
    res = pd.DataFrame(out)
    return res.sort_values("相似度", ascending=False).reset_index(drop=True) if not res.empty else res


# ======================================================================
# 圖表
# ======================================================================
def add_price_range(df: pd.DataFrame, bins: int = 5):
    """以『兩平台合併後』的分位數切價格區間，確保各平台使用相同區間才能比較。"""
    d = df.copy()
    prices = d["價格"]
    edges = sorted({int(x) for x in prices.quantile([i / bins for i in range(bins + 1)]).round()})
    if len(edges) < 2:
        label = f"${edges[0]:,}"
        d["價格區間"] = label
        return d, [label]
    labels = [f"${edges[i]:,}–{edges[i + 1]:,}" for i in range(len(edges) - 1)]
    d["價格區間"] = pd.cut(prices, bins=edges, labels=labels, include_lowest=True).astype(str)
    return d, labels


def pick_n(label: str, max_n: int, default: int) -> int:
    if max_n <= 5:
        return max_n
    return st.slider(label, 5, max_n, min(default, max_n))


def chart_box(df, kw):
    fig = px.box(df, x="平台", y="價格", color="平台", points="all", hover_name="品名",
                 color_discrete_map=COLORS, title=f"「{kw}」各平台價格分布（箱型圖）")
    fig.update_layout(showlegend=False)
    return fig


def chart_hist(df, kw):
    fig = px.histogram(df, x="價格", color="平台", nbins=30, barmode="overlay", opacity=0.65,
                       color_discrete_map=COLORS, title=f"「{kw}」價格直方圖")
    fig.update_layout(yaxis_title="商品數")
    return fig


def chart_line(df, kw):
    d = df.copy()
    d["序號"] = d.groupby("平台").cumcount() + 1
    fig = px.line(d, x="序號", y="價格", color="平台", markers=True, hover_name="品名",
                  color_discrete_map=COLORS, title=f"「{kw}」商品售價（依搜尋結果順序）")
    for p, m in d.groupby("平台")["價格"].mean().items():
        fig.add_hline(y=m, line_dash="dash", line_color=COLORS[p],
                      annotation_text=f"{p} 平均 {m:,.0f}", annotation_position="top left")
    return fig


def chart_pie(df, kw):
    d, labels = add_price_range(df)
    plats = [p for p in PLATFORMS if p in set(d["平台"])]
    palette = px.colors.qualitative.Set2
    cmap = {lb: palette[i % len(palette)] for i, lb in enumerate(labels)}
    fig = make_subplots(rows=1, cols=len(plats), specs=[[{"type": "domain"}] * len(plats)],
                        subplot_titles=plats)
    for k, p in enumerate(plats, start=1):
        cnt = d[d["平台"] == p]["價格區間"].value_counts().reindex(labels, fill_value=0)
        fig.add_trace(
            go.Pie(labels=list(cnt.index), values=list(cnt.values), hole=0.35, sort=False,
                   marker=dict(colors=[cmap[lb] for lb in cnt.index]),
                   textinfo="percent+value", showlegend=(k == 1), name=p),
            row=1, col=k,
        )
    fig.update_layout(title=f"「{kw}」價格區間占比（兩平台使用相同區間）")
    return fig


def chart_sunburst(df, kw, n):
    d, _ = add_price_range(df)
    d = d.groupby("平台", sort=False).head(n).reset_index(drop=True)
    d["商品"] = [f"{name[:14]}… #{i + 1}" for i, name in enumerate(d["品名"])]
    return px.sunburst(d, path=["平台", "價格區間", "商品"], values="價格", color="平台",
                       color_discrete_map=COLORS, title=f"「{kw}」平台 › 價格區間 › 商品")


# ======================================================================
# 側邊欄：查詢條件
# ======================================================================
st.title("🛒 PChome × MOMO 比價系統")

with st.sidebar:
    st.header("查詢條件")
    keyword = st.text_input("商品關鍵字", value="行動電源")
    pages = st.number_input("爬取頁數（每平台）", min_value=1, max_value=20, value=2, step=1)
    platforms = st.multiselect("比價平台", PLATFORMS, default=PLATFORMS)
    with st.expander("進階設定"):
        delay = st.slider("每頁間隔秒數", 0.0, 10.0, 1.0, 0.5)
        sort_label = st.selectbox("PChome 排序方式", list(PCHOME_SORTS))
        force = st.checkbox("忽略快取，強制重新抓取", value=False)
    run = st.button("🔍 開始查詢", type="primary", **STRETCH)

if run:
    kw_in = keyword.strip()
    if not kw_in:
        st.warning("請輸入商品關鍵字")
        st.stop()
    if not platforms:
        st.warning("請至少選擇一個平台")
        st.stop()
    if force:
        crawl_all.clear()
    with st.spinner(f"正在並行抓取「{kw_in}」每平台前 {int(pages)} 頁…"):
        raw, errors = crawl_all(kw_in, int(pages), tuple(platforms),
                                PCHOME_SORTS[sort_label], float(delay))
    st.session_state["result"] = {"kw": kw_in, "df": raw, "errors": errors,
                                  "time": datetime.now()}

res = st.session_state.get("result")
if res is None:
    st.info("請在左側輸入關鍵字、頁數並選擇平台，然後按「開始查詢」。")
    st.stop()

kw, raw, errors = res["kw"], res["df"], res["errors"]
for msg in errors:
    st.warning(msg)
if raw.empty:
    st.error("沒有取得任何商品資料，可能被網站擋下或關鍵字無結果。")
    st.stop()

# ---------- 結果篩選（不需重新爬取） ----------
df = raw
with st.sidebar:
    st.divider()
    st.subheader("結果篩選")
    lo, hi = int(raw["價格"].min()), int(raw["價格"].max())
    if lo < hi:
        rng = st.slider("價格範圍", lo, hi, (lo, hi))
        df = df[df["價格"].between(*rng)]
    exclude = st.text_input("排除關鍵字（空白分隔）", placeholder="例如：保護殼 線材")
    words = exclude.split()
    if words:
        pat = "|".join(re.escape(w) for w in words)
        df = df[~df["品名"].str.contains(pat, case=False, regex=True)]
df = df.reset_index(drop=True)

if df.empty:
    st.warning("篩選後沒有商品，請放寬條件。")
    st.stop()

present = [p for p in PLATFORMS if p in set(df["平台"])]
both = len(present) == 2
st.caption(f"查詢「{kw}」｜{res['time']:%Y-%m-%d %H:%M:%S}｜共 {len(df):,} 筆商品")

# ======================================================================
# 統計指標
# ======================================================================
stats = (df.groupby("平台")["價格"]
         .agg(商品數="count", 平均價="mean", 中位數="median", 最低價="min", 最高價="max")
         .round(0).astype(int).reindex(present))

cols = st.columns(len(present))
for col, p in zip(cols, present):
    delta = None
    if both:
        other = stats.loc[[x for x in present if x != p][0], "平均價"]
        diff = int(stats.loc[p, "平均價"] - other)
        if diff:
            delta = f"{diff:+,} vs {[x for x in present if x != p][0]}"
    col.metric(f"{p}｜平均價（{stats.loc[p, '商品數']} 筆）",
               f"${stats.loc[p, '平均價']:,}", delta=delta, delta_color="inverse")

tab_over, tab_chart, tab_match, tab_detail = st.tabs(
    ["📊 比價總覽", "📈 圖表", "🔗 相似商品比價", "📋 商品明細"]
)

# ---------- 比價總覽 ----------
with tab_over:
    st.dataframe(stats, **STRETCH)
    if both:
        a, b = present
        ma, mb = stats.loc[a, "中位數"], stats.loc[b, "中位數"]
        lower, higher = (a, b) if ma <= mb else (b, a)
        pct = (stats.loc[higher, "中位數"] - stats.loc[lower, "中位數"]) / stats.loc[higher, "中位數"] * 100
        st.info(f"以搜尋結果的**中位數**來看，**{lower}** 較低（約低 {pct:.1f}%）。"
                "此為整體價位比較，同款商品請看「相似商品比價」。")
    st.subheader("各平台最低價 Top 5")
    cols = st.columns(len(present))
    for col, p in zip(cols, present):
        with col:
            st.markdown(f"**{p}**")
            top = df[df["平台"] == p].nsmallest(5, "價格")[["品名", "價格", "連結"]]
            st.dataframe(top, hide_index=True, **STRETCH,
                         column_config={"連結": st.column_config.LinkColumn("連結", display_text="前往")})

# ---------- 圖表 ----------
with tab_chart:
    kind = st.radio("圖表類型", ["箱型圖", "直方圖", "折線圖", "圓餅圖", "旭日圖"], horizontal=True)
    if kind == "箱型圖":
        fig = chart_box(df, kw)
    elif kind == "直方圖":
        fig = chart_hist(df, kw)
    elif kind == "折線圖":
        fig = chart_line(df, kw)
    elif kind == "圓餅圖":
        fig = chart_pie(df, kw)
    else:
        n = pick_n("每平台顯示商品數", int(df.groupby("平台").size().max()), 30)
        fig = chart_sunburst(df, kw, n)
    fig.update_layout(height=620, font=dict(size=14))
    st.plotly_chart(fig, key=f"chart_{kind}", **STRETCH)

# ---------- 相似商品比價 ----------
with tab_match:
    if not both:
        st.info("需要同時有 PChome 與 MOMO 的資料才能配對比價。")
    else:
        thr = st.slider("名稱相似度門檻（越高越嚴格）", 0.2, 0.9, 0.45, 0.05)
        pairs = match_products(df, thr)
        if pairs.empty:
            st.warning("沒有找到相似商品，請調低門檻或增加爬取頁數。")
        else:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("配對商品數", len(pairs))
            m2.metric("PChome 較便宜", int((pairs["較便宜"] == "PChome").sum()))
            m3.metric("MOMO 較便宜", int((pairs["較便宜"] == "MOMO").sum()))
            m4.metric("平均價差 (MOMO−PChome)", f"{pairs['價差(MOMO-PChome)'].mean():+,.0f}")

            lim = [min(pairs["PChome價格"].min(), pairs["MOMO價格"].min()),
                   max(pairs["PChome價格"].max(), pairs["MOMO價格"].max())]
            sc = px.scatter(pairs, x="PChome價格", y="MOMO價格", color="較便宜",
                            color_discrete_map=COLORS,
                            hover_data=["PChome品名", "MOMO品名", "相似度"],
                            title="配對商品價格對照（虛線上＝兩平台同價）")
            sc.add_shape(type="line", x0=lim[0], y0=lim[0], x1=lim[1], y1=lim[1],
                         line=dict(dash="dot", color="gray"))
            sc.update_layout(height=520)
            st.plotly_chart(sc, key="chart_pairs", **STRETCH)

            st.dataframe(
                pairs, hide_index=True, **STRETCH,
                column_config={
                    "PChome連結": st.column_config.LinkColumn("PChome連結", display_text="前往"),
                    "MOMO連結": st.column_config.LinkColumn("MOMO連結", display_text="前往"),
                    "PChome價格": st.column_config.NumberColumn(format="$%d"),
                    "MOMO價格": st.column_config.NumberColumn(format="$%d"),
                    "價差(MOMO-PChome)": st.column_config.NumberColumn(format="%+d"),
                },
            )
            st.caption("⚠️ 配對依名稱相似度與規格字樣（如 10000mAh、20W）自動判斷，"
                       "可能有誤，下單前請點連結確認是否為同款。")
            safe = re.sub(r'[\\/:*?"<>|]', "_", kw)
            st.download_button("⬇️ 下載配對比價 CSV",
                               pairs.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"比價_{safe}_{datetime.now():%Y%m%d}.csv", mime="text/csv")

# ---------- 商品明細 ----------
with tab_detail:
    view = st.multiselect("顯示平台", present, default=present)
    show = df[df["平台"].isin(view)]
    st.dataframe(
        show, hide_index=True, **STRETCH,
        column_config={
            "連結": st.column_config.LinkColumn("連結", display_text="前往商品"),
            "價格": st.column_config.NumberColumn(format="$%d"),
            "原價": st.column_config.NumberColumn(format="$%d"),
        },
    )
    safe = re.sub(r'[\\/:*?"<>|]', "_", kw)
    st.download_button("⬇️ 下載商品明細 CSV", show.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"商品明細_{safe}_{datetime.now():%Y%m%d}.csv", mime="text/csv")
