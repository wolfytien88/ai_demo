from pathlib import Path
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "islemart_orders_wide.csv"
REPORT = ROOT / "islemart_order_eda_report.md"
CHARTS = {
    "monthly": ROOT / "eda_monthly_revenue.png",
    "category": ROOT / "eda_category_revenue.png",
    "payment": ROOT / "eda_payment_method.png",
    "city": ROOT / "eda_city_distribution.png",
}
FILES = {
    "customers": "islemart_customers_dataset.csv",
    "geolocation": "islemart_geolocation_dataset.csv",
    "items": "islemart_order_items_dataset.csv",
    "payments": "islemart_order_payments_dataset.csv",
    "reviews": "islemart_order_reviews_dataset.csv",
    "orders": "islemart_orders_dataset.csv",
    "products": "islemart_products_dataset.csv",
    "sellers": "islemart_sellers_dataset.csv",
    "translation": "product_category_name_translation.csv",
}
COLOR = "#167C80"
ACCENT = "#E58A32"


def load_tables():
    return {name: pd.read_csv(ROOT / filename) for name, filename in FILES.items()}


def json_list(values):
    cleaned = sorted({str(value) for value in values if pd.notna(value) and str(value).strip()})
    return json.dumps(cleaned, ensure_ascii=False)


def json_category_revenue(frame):
    records = frame[["category_label", "line_revenue"]].to_dict("records")
    return json.dumps(
        [{"category": row["category_label"], "revenue": float(row["line_revenue"])} for row in records],
        ensure_ascii=False,
    )


def json_payment_totals(frame):
    totals = frame.groupby("payment_type", dropna=False)["payment_value"].sum().sort_index()
    return json.dumps(
        [{"payment_type": str(key), "amount": float(value)} for key, value in totals.items()],
        ensure_ascii=False,
    )


def json_nonempty(values):
    cleaned = sorted({str(value).strip() for value in values if pd.notna(value) and str(value).strip()})
    return json.dumps(cleaned, ensure_ascii=False)


def mode_or_missing(values):
    modes = values.dropna().mode()
    return modes.iloc[0] if not modes.empty else pd.NA


def sample_json(row):
    return json.dumps(row, ensure_ascii=False, default=str)


def make_aggregates(tables, ledger):
    # Collapse the non-unique postal-code lookup before joining it to customers.
    geo = tables["geolocation"]
    geo_by_zip = geo.groupby("geolocation_zip_code_prefix", as_index=False).agg(
        geolocation_point_count=("geolocation_zip_code_prefix", "size"),
        geolocation_lat_mean=("geolocation_lat", "mean"),
        geolocation_lng_mean=("geolocation_lng", "mean"),
        geolocation_city=("geolocation_city", mode_or_missing),
        geolocation_county=("geolocation_county", mode_or_missing),
    )
    ledger.append(("地理資料依郵遞區號彙總", len(geo), len(geo_by_zip), 0, len(geo) - len(geo_by_zip), "以眾數保存區/縣市，另保留座標筆數與平均座標。"))

    translation = tables["translation"]
    products = tables["products"].merge(
        translation,
        on="product_category_name",
        how="left",
        validate="many_to_one",
    )
    products["category_label"] = products["product_category_name_english"]
    missing_or_untranslated = products["category_label"].isna()
    products.loc[missing_or_untranslated, "category_label"] = products.loc[missing_or_untranslated, "product_category_name"]
    products.loc[products["product_category_name"].isna(), "category_label"] = "未分類"
    untranslated = products["product_category_name"].notna() & products["product_category_name_english"].isna()
    products.loc[untranslated, "category_label"] = products.loc[untranslated, "category_label"].astype(str) + "（未翻譯）"
    ledger.append(("商品主檔左連分類翻譯", len(tables["products"]), len(products), 0, 0, "保留未分類與未翻譯商品，不因翻譯缺漏排除商品。"))

    sellers = tables["sellers"][["seller_id", "seller_city", "seller_county"]].copy()
    item_rows = tables["items"].merge(
        products[["product_id", "category_label"]],
        on="product_id",
        how="left",
        validate="many_to_one",
    ).merge(sellers, on="seller_id", how="left", validate="many_to_one")
    item_rows["category_label"] = item_rows["category_label"].fillna("商品主檔未命中")
    item_rows["line_revenue"] = item_rows["price"] + item_rows["freight_value"]
    ledger.append(("明細連商品/賣家主檔", len(tables["items"]), len(item_rows), 0, 0, "商品與賣家均採 many-to-one 左連接；明細列完整保留。"))

    category_totals = item_rows.groupby(["order_id", "category_label"], as_index=False)["line_revenue"].sum()
    category_json = category_totals.groupby("order_id", sort=False).apply(json_category_revenue, include_groups=False).rename("category_revenue_json")
    item_agg = item_rows.groupby("order_id").agg(
        item_count=("order_item_id", "size"),
        unique_product_count=("product_id", "nunique"),
        unique_seller_count=("seller_id", "nunique"),
        item_price_total=("price", "sum"),
        freight_total=("freight_value", "sum"),
        product_ids_json=("product_id", json_list),
        seller_ids_json=("seller_id", json_list),
        seller_cities_json=("seller_city", json_list),
        seller_counties_json=("seller_county", json_list),
        product_categories_json=("category_label", json_list),
    )
    item_agg["category_revenue_json"] = category_json
    item_agg["order_amount_price_plus_freight"] = item_agg["item_price_total"] + item_agg["freight_total"]
    ledger.append(("訂單明細彙總", len(item_rows), len(item_agg), 0, len(item_rows) - len(item_agg), "所有商品列均納入訂單層級彙總，列數差是彙總折疊而非排除。"))

    payment_rows = tables["payments"]
    payment_agg = payment_rows.groupby("order_id").agg(
        payment_count=("payment_sequential", "size"),
        payment_total=("payment_value", "sum"),
        payment_methods_json=("payment_type", json_list),
    )
    payment_json = payment_rows.groupby("order_id", sort=False).apply(json_payment_totals, include_groups=False).rename("payment_method_totals_json")
    payment_agg["payment_method_totals_json"] = payment_json
    ledger.append(("訂單付款彙總", len(payment_rows), len(payment_agg), 0, len(payment_rows) - len(payment_agg), "保留付款總額、付款次數及付款方式拆分。"))

    reviews = tables["reviews"]
    review_dedup = reviews.drop_duplicates().copy()
    duplicate_count = len(reviews) - len(review_dedup)
    duplicate_examples = reviews.loc[reviews.duplicated(keep="first")]
    review_dedup["review_creation_date"] = pd.to_datetime(review_dedup["review_creation_date"], errors="coerce")
    review_dedup["review_answer_timestamp"] = pd.to_datetime(review_dedup["review_answer_timestamp"], errors="coerce")
    review_agg = review_dedup.groupby("order_id").agg(
        review_count=("review_id", "size"),
        review_score_mean=("review_score", "mean"),
        review_score_min=("review_score", "min"),
        review_score_max=("review_score", "max"),
        review_ids_json=("review_id", json_list),
        review_titles_json=("review_comment_title", json_nonempty),
        review_messages_json=("review_comment_message", json_nonempty),
        review_creation_first=("review_creation_date", "min"),
        review_answer_last=("review_answer_timestamp", "max"),
    )
    ledger.append(("評論完全重複列去重", len(reviews), len(review_dedup), duplicate_count, 0, "只移除所有欄位完全相同的額外列。"))
    ledger.append(("訂單評論彙總", len(review_dedup), len(review_agg), 0, len(review_dedup) - len(review_agg), "多筆評論彙總成評論數、分數摘要與文字清單。"))

    customers = tables["customers"].merge(
        geo_by_zip,
        left_on="customer_zip_code_prefix",
        right_on="geolocation_zip_code_prefix",
        how="left",
        validate="many_to_one",
    )
    ledger.append(("客戶左連郵遞區號地理摘要", len(tables["customers"]), len(customers), 0, 0, "每個郵遞區號先收斂為一列，避免客戶列數倍增。"))

    orders = tables["orders"].copy()
    date_columns = [column for column in orders.columns if column.endswith(("timestamp", "_at", "_date"))]
    for column in date_columns:
        orders[column] = pd.to_datetime(orders[column], errors="coerce")
    orders = orders.merge(customers, on="customer_id", how="left", validate="one_to_one")
    orders = orders.merge(item_agg, on="order_id", how="left", validate="one_to_one")
    orders = orders.merge(payment_agg, on="order_id", how="left", validate="one_to_one")
    orders = orders.merge(review_agg, on="order_id", how="left", validate="one_to_one")
    orders["recognized_revenue_delivered"] = orders["order_amount_price_plus_freight"].where(orders["order_status"].eq("delivered"), 0)
    orders["payment_amount_delta"] = orders["order_amount_price_plus_freight"] - orders["payment_total"]
    orders["payment_reconciliation_mismatch"] = orders["payment_amount_delta"].abs().gt(1)
    orders["delivery_chronology_issue"] = (
        orders["order_delivered_customer_date"].notna()
        & orders["order_delivered_carrier_date"].notna()
        & (orders["order_delivered_customer_date"] < orders["order_delivered_carrier_date"])
    )
    orders["delivered_timestamp_missing"] = (
        orders["order_status"].eq("delivered")
        & orders["order_delivered_customer_date"].isna()
    )
    ledger.append(("訂單合併客戶/明細/付款/評論摘要", len(tables["orders"]), len(orders), 0, 0, "訂單為主表，所有摘要均左連接；不會因子表缺列而丟棄訂單。"))
    return orders, item_rows, payment_rows, review_dedup, duplicate_count, duplicate_examples, products, ledger


def set_plot_style():
    plt.rcParams.update({
        "font.family": "Microsoft JhengHei",
        "axes.unicode_minus": False,
        "figure.facecolor": "#FBFAF7",
        "axes.facecolor": "#FBFAF7",
        "axes.edgecolor": "#BCC7C5",
        "axes.labelcolor": "#263B3B",
        "text.color": "#263B3B",
        "xtick.color": "#435454",
        "ytick.color": "#435454",
        "axes.titleweight": "semibold",
    })


def format_axis(axis):
    axis.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value:,.0f}"))
    axis.grid(axis="y", color="#DCE3E0", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)


def make_charts(wide, item_rows, payment_rows):
    delivered = wide[wide["order_status"].eq("delivered")].copy()
    delivered_ids = set(delivered["order_id"])
    delivered_items = item_rows[item_rows["order_id"].isin(delivered_ids)].copy()
    delivered_payments = payment_rows[payment_rows["order_id"].isin(delivered_ids)].copy()
    counts = {
        "orders": (len(wide), len(delivered), len(wide) - len(delivered)),
        "items": (len(item_rows), len(delivered_items), len(item_rows) - len(delivered_items)),
        "payments": (len(payment_rows), len(delivered_payments), len(payment_rows) - len(delivered_payments)),
    }
    set_plot_style()

    monthly = delivered.groupby(delivered["order_purchase_timestamp"].dt.to_period("M"))["recognized_revenue_delivered"].sum()
    fig, axis = plt.subplots(figsize=(11, 5.2), layout="constrained")
    axis.plot(monthly.index.astype(str), monthly.values, color=COLOR, linewidth=2.4, marker="o", markersize=4)
    axis.fill_between(range(len(monthly)), monthly.values, color=COLOR, alpha=0.10)
    axis.set_title("月營收趨勢｜已送達訂單，售價 + 運費", loc="left", pad=14)
    axis.set_xlabel("下單月份")
    axis.set_ylabel("營收（原資料金額單位）")
    axis.tick_params(axis="x", rotation=45)
    format_axis(axis)
    fig.savefig(CHARTS["monthly"], dpi=170)
    plt.close(fig)

    category_sales = delivered_items.groupby("category_label")["line_revenue"].sum().sort_values().tail(18)
    fig, axis = plt.subplots(figsize=(10, max(5.5, len(category_sales) * 0.34)), layout="constrained")
    axis.barh(range(len(category_sales)), category_sales.values, color=COLOR)
    axis.set_title("類別營收｜已送達訂單，售價 + 運費", loc="left", pad=14)
    axis.set_xlabel("營收（原資料金額單位）")
    axis.set_ylabel("")
    format_axis(axis)
    axis.set_yticks(range(len(category_sales)), labels=category_sales.index.astype(str))
    fig.savefig(CHARTS["category"], dpi=170)
    plt.close(fig)

    payment_sales = delivered_payments.groupby("payment_type")["payment_value"].sum().sort_values()
    fig, axis = plt.subplots(figsize=(9, 5.4), layout="constrained")
    bars = axis.bar(payment_sales.index, payment_sales.values, color=[ACCENT if index == len(payment_sales) - 1 else COLOR for index in range(len(payment_sales))])
    axis.set_title("付款方式分布｜付款金額", loc="left", pad=14)
    axis.set_xlabel("付款方式")
    axis.set_ylabel("付款金額（原資料金額單位）")
    axis.tick_params(axis="x", rotation=18)
    format_axis(axis)
    axis.bar_label(bars, labels=[f"{value:,.0f}" for value in payment_sales.values], padding=3, fontsize=8)
    fig.savefig(CHARTS["payment"], dpi=170)
    plt.close(fig)

    city_orders = delivered.assign(city_label=delivered["customer_county"].fillna("未提供") + "／" + delivered["customer_city"].fillna("未提供"))
    city_counts = city_orders.groupby("city_label").size().sort_values()
    fig, axis = plt.subplots(figsize=(10, max(5.5, len(city_counts) * 0.34)), layout="constrained")
    axis.barh(range(len(city_counts)), city_counts.values, color=ACCENT)
    axis.set_title("縣市/城市訂單分布｜已送達訂單", loc="left", pad=14)
    axis.set_xlabel("訂單數")
    axis.set_ylabel("")
    format_axis(axis)
    axis.xaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value:,.0f}"))
    axis.set_yticks(range(len(city_counts)), labels=city_counts.index.astype(str))
    fig.savefig(CHARTS["city"], dpi=170)
    plt.close(fig)
    return counts, delivered, delivered_items, delivered_payments


def compute_quality(tables, wide, products, duplicate_count):
    untranslated = products["product_category_name"].notna() & products["product_category_name_english"].isna()
    return [
        ("評論完全重複列", duplicate_count, "只刪除全欄位完全相同的額外列；評論已彙總。", "review_count 保留每張訂單的有效評論數。"),
        ("商品分類缺值", int(products["product_category_name"].isna().sum()), "保留商品，類別標為「未分類」。", "商品及訂單不排除。"),
        ("商品分類未翻譯", int(untranslated.sum()), "保留商品，使用原分類名稱並加上「（未翻譯）」。", "商品及訂單不排除。"),
        ("送達時間早於交物流時間", int(wide["delivery_chronology_issue"].sum()), "保留原始時間，不自行校正；新增 delivery_chronology_issue。", "不排除訂單。"),
        ("已送達但缺實際送達時間", int(wide["delivered_timestamp_missing"].sum()), "保留訂單並標記 delivered_timestamp_missing。", "營收判斷依 order_status，不依缺漏時間。"),
        ("商品/運費與付款總額差異 > 1", int(wide["payment_reconciliation_mismatch"].sum()), "保留兩套金額並新增對帳差額/旗標；不以付款金額覆蓋營收。", "不排除訂單；折扣/退款定義未提供。"),
    ]


def write_report(tables, wide, ledger, chart_counts, quality, duplicate_examples):
    lines = [
        "# 訂單寬表與 EDA 執行報告",
        "",
        "## 營收定義",
        "",
        "主要口徑：每筆商品明細的 `price + freight_value` 加總為訂單含運費金額；僅 `order_status = delivered` 的訂單列入本報告 EDA 的認列營收。取消、不可用、處理中及運送中的訂單保留在寬表，但不列入已送達營收圖。資料沒有折扣/退款欄位或幣別欄位，因此不推算淨額、不假設幣別。付款金額獨立保留，不用來取代商品明細營收。",
        "",
        "替代口徑也保留在寬表：`item_price_total` 為不含運費的商品銷售額，`freight_total` 為運費，`order_amount_price_plus_freight` 為全部狀態的商品+運費金額，`recognized_revenue_delivered` 為已送達訂單認列值。",
        "",
        "## 處理步驟與列數",
        "",
        "列數縮減若標示為「彙總折疊」，代表多列被彙總保留，不是排除；只有排除欄記錄真正移除的來源列。",
        "",
        "| 步驟 | 處理前 | 處理後 | 排除筆數 | 彙總折疊 | 說明 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    lines.extend(f"| {name} | {before:,} | {after:,} | {excluded:,} | {collapsed:,} | {detail} |" for name, before, after, excluded, collapsed, detail in ledger)
    lines.append(f"| 最終一列一訂單寬表 | {len(tables['orders']):,} | {len(wide):,} | {len(tables['orders']) - len(wide):,} | 0 | 以 orders 為主表，order_id 唯一。 |")
    lines.extend([
        "",
        "## 陷阱紀錄與處理結果",
        "",
        "工作區沒有獨立的陷阱紀錄表；以下依前次 `islemart_data_quality_report.md` 中檢出的六項問題處理。",
        "",
        "| 資料問題 | 筆數 | 本次處理 | 排除筆數 |",
        "|---|---:|---|---:|",
    ])
    lines.extend(f"| {name} | {count:,} | {action} {note} | {count if name == '評論完全重複列' else 0:,} |" for name, count, action, note in quality)
    if not duplicate_examples.empty:
        lines.extend(["", "評論重複列範例：", "", "```json", sample_json(duplicate_examples.iloc[0].to_dict()), "```"])
    lines.extend([
        "",
        "## EDA 圖表與樣本數",
        "",
        "以下四張圖均以已送達訂單為營收/訂單範圍；其他狀態的列只從 EDA 圖表樣本排除，不從寬表刪除。",
        "",
    ])
    eda_rows = [
        ("月營收趨勢", *chart_counts["orders"], "訂單列依下單月份彙總認列營收。"),
        ("類別營收", *chart_counts["items"], "商品明細列依商品類別彙總；含未分類/未翻譯標籤。"),
        ("付款方式", *chart_counts["payments"], "付款明細列依付款方式加總 payment_value。"),
        ("縣市分布", *chart_counts["orders"], "已送達訂單依客戶縣市/城市計數。"),
    ]
    lines.extend(["| 圖表 | 處理前 | 納入分析 | 排除筆數 | 說明 |", "|---|---:|---:|---:|---|"])
    lines.extend(f"| {name} | {before:,} | {included:,} | {excluded:,} | {detail} |" for name, before, included, excluded, detail in eda_rows)
    lines.extend(["", "| 圖表 | 檔案 |", "|---|---|"])
    chart_titles = {
        "monthly": "月營收趨勢",
        "category": "類別營收",
        "payment": "付款方式分布",
        "city": "縣市/城市分布",
    }
    lines.extend(f"| {chart_titles[name]} | ![{chart_titles[name]}]({path.name}) |" for name, path in CHARTS.items())
    lines.extend(["", f"寬表：`{OUTPUT.name}`（{len(wide):,} 列，{wide['order_id'].nunique():,} 個唯一訂單）。", ""])
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main():
    tables = load_tables()
    ledger = []
    wide, item_rows, payment_rows, review_dedup, duplicate_count, duplicate_examples, products, ledger = make_aggregates(tables, ledger)
    if wide["order_id"].duplicated().any():
        raise ValueError("最終寬表 order_id 不唯一，停止輸出。")
    if len(wide) != len(tables["orders"]):
        raise ValueError("最終寬表列數與訂單主表不同，停止輸出。")
    wide.to_csv(OUTPUT, index=False, encoding="utf-8-sig")

    chart_counts, _, _, _ = make_charts(wide, item_rows, payment_rows)
    quality = compute_quality(tables, wide, products, duplicate_count)
    write_report(tables, wide, ledger, chart_counts, quality, duplicate_examples)
    print(f"寬表：{OUTPUT.name} ({len(wide):,} rows, {wide['order_id'].nunique():,} unique orders)")
    print(f"報告：{REPORT.name}")
    print("圖表：" + ", ".join(path.name for path in CHARTS.values()))
    print(f"精確重複評論排除：{duplicate_count:,}")
    for name, before, included, excluded in [
        ("月趨勢/縣市", *chart_counts["orders"]),
        ("類別營收", *chart_counts["items"]),
        ("付款方式", *chart_counts["payments"]),
    ]:
        print(f"{name}: {before:,} -> {included:,}; excluded={excluded:,}")


if __name__ == "__main__":
    main()