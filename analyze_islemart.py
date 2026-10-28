from pathlib import Path
import json

import pandas as pd


ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "islemart_data_quality_report.md"
FILES = {
    "customers": "islemart_customers_dataset.csv",
    "geolocation": "islemart_geolocation_dataset.csv",
    "order_items": "islemart_order_items_dataset.csv",
    "order_payments": "islemart_order_payments_dataset.csv",
    "order_reviews": "islemart_order_reviews_dataset.csv",
    "orders": "islemart_orders_dataset.csv",
    "products": "islemart_products_dataset.csv",
    "sellers": "islemart_sellers_dataset.csv",
    "translation": "product_category_name_translation.csv",
}
GRAINS = {
    "customers": "一筆客戶訂單識別紀錄（customer_id）；customer_unique_id 可跨多筆紀錄重複。",
    "geolocation": "一筆郵遞區號對應的地理座標/行政區紀錄；同一郵遞區號可有多個座標。",
    "order_items": "一筆訂單中的一個商品項次。",
    "order_payments": "一筆訂單中的一個付款序號/付款方式紀錄。",
    "order_reviews": "一筆訂單的一筆評論紀錄；目前有完全重複列。",
    "orders": "一筆訂單。",
    "products": "一筆商品主檔。",
    "sellers": "一筆賣家主檔。",
    "translation": "一筆商品分類名稱翻譯對照。",
}
KEYS = {
    "customers": [("customer_id",)],
    "geolocation": [],
    "order_items": [("order_id", "order_item_id")],
    "order_payments": [("order_id", "payment_sequential")],
    "order_reviews": [("review_id",), ("review_id", "order_id")],
    "orders": [("order_id",)],
    "products": [("product_id",)],
    "sellers": [("seller_id",)],
    "translation": [("product_category_name",)],
}


def load_tables():
    return {name: pd.read_csv(ROOT / filename) for name, filename in FILES.items()}


def fmt_example(row):
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if not isinstance(value, (list, dict)) and pd.isna(value):
            return None
        if hasattr(value, "item"):
            value = value.item()
        return value

    return json.dumps(normalize(row), ensure_ascii=False, default=str)


def key_summary(name, frame):
    if not KEYS[name]:
        return "未找到具業務意義的唯一鍵；郵遞區號重複。完整列目前唯一。"
    descriptions = []
    for columns in KEYS[name]:
        duplicates = int(frame.duplicated(list(columns)).sum())
        key_name = " + ".join(columns)
        status = "唯一" if duplicates == 0 else f"不唯一，多出 {duplicates:,} 筆重複列"
        descriptions.append(f"`{key_name}`：{status}")
    suffix = "。目前沒有確認到唯一主鍵。" if name == "order_reviews" else "。"
    return "; ".join(descriptions) + suffix


def schema_section(tables):
    lines = ["## 1. 列數、欄位、型態與缺值", ""]
    for name, frame in tables.items():
        lines.extend([f"### {FILES[name]}（{len(frame):,} 列）", ""])
        lines.extend(["| 欄位 | 推斷型態 | 缺值 | 缺值比例 |", "|---|---:|---:|---:|"])
        for column in frame.columns:
            missing = int(frame[column].isna().sum())
            ratio = missing / len(frame) if len(frame) else 0
            lines.append(f"| `{column}` | `{frame[column].dtype}` | {missing:,} | {ratio:.2%} |")
        lines.append("")
    return lines


def quality_issues(tables):
    issues = []
    reviews = tables["order_reviews"]
    duplicate_rows = reviews.duplicated(keep="first")
    if duplicate_rows.any():
        issues.append(("評論資料有完全重複列", int(duplicate_rows.sum()), "以全部欄位判斷；筆數為重複的額外列數。", reviews.loc[duplicate_rows].iloc[0].to_dict()))

    products = tables["products"]
    missing_category = products["product_category_name"].isna()
    if missing_category.any():
        issues.append(("商品缺少分類", int(missing_category.sum()), "product_category_name 為空。", products.loc[missing_category].iloc[0].to_dict()))

    translation_names = set(tables["translation"]["product_category_name"].dropna())
    unmapped = products["product_category_name"].notna() & ~products["product_category_name"].isin(translation_names)
    if unmapped.any():
        category_count = products.loc[unmapped, "product_category_name"].nunique()
        issues.append(("商品分類沒有翻譯對照", int(unmapped.sum()), f"共有 {category_count} 個分類名稱未出現在翻譯表。", products.loc[unmapped].iloc[0].to_dict()))

    orders = tables["orders"].copy()
    date_columns = [column for column in orders.columns if column.endswith(("timestamp", "_at", "_date"))]
    for column in date_columns:
        orders[column] = pd.to_datetime(orders[column], errors="coerce")
    inversion = (
        orders["order_delivered_customer_date"].notna()
        & orders["order_delivered_carrier_date"].notna()
        & (orders["order_delivered_customer_date"] < orders["order_delivered_carrier_date"])
    )
    if inversion.any():
        example_columns = ["order_id", "order_delivered_carrier_date", "order_delivered_customer_date"]
        issues.append(("送達時間早於交物流時間", int(inversion.sum()), "order_delivered_customer_date < order_delivered_carrier_date。", orders.loc[inversion, example_columns].iloc[0].to_dict()))

    delivered_missing = orders["order_status"].eq("delivered") & orders["order_delivered_customer_date"].isna()
    if delivered_missing.any():
        issues.append(("已送達訂單缺少實際送達時間", int(delivered_missing.sum()), "order_status = delivered，但 order_delivered_customer_date 為空。", orders.loc[delivered_missing].iloc[0].to_dict()))

    items = tables["order_items"].assign(item_total=lambda frame: frame["price"] + frame["freight_value"]).groupby("order_id")["item_total"].sum()
    payments = tables["order_payments"].groupby("order_id")["payment_value"].sum()
    shared_orders = items.index.intersection(payments.index)
    delta = items.loc[shared_orders] - payments.loc[shared_orders]
    mismatched = delta.abs().gt(1)
    if mismatched.any():
        order_id = delta.loc[mismatched].index[0]
        payment_example = tables["order_payments"].loc[tables["order_payments"]["order_id"].eq(order_id)].iloc[0].to_dict()
        example = {"order_id": order_id, "items_plus_freight": items.loc[order_id], "payment_total": payments.loc[order_id], "payment_example": payment_example}
        rule = "每筆明細 price + freight_value 加總與 payment_value 加總比較，差額絕對值 > 1；需確認折扣/退款等業務規則。"
        issues.append(("訂單商品/運費與付款合計不一致", int(mismatched.sum()), rule, example))
    return issues


def relationship_section(tables):
    def coverage(source, source_column, target, target_column):
        source_values = tables[source][source_column]
        target_values = set(tables[target][target_column].dropna())
        matched = int(source_values.isin(target_values).sum())
        return f"外鍵命中 {matched:,}/{len(source_values):,}（{matched / len(source_values):.2%}）"

    review_order_count = tables["order_reviews"]["order_id"].nunique()
    rows = [
        ("customers", "orders", "1:1", f"customer_id；兩側唯一；{coverage('orders', 'customer_id', 'customers', 'customer_id')}"),
        ("orders", "order_items", "1:N", f"order_id；明細複合鍵唯一；{coverage('order_items', 'order_id', 'orders', 'order_id')}"),
        ("orders", "order_payments", "1:N", f"order_id；付款複合鍵唯一；{coverage('order_payments', 'order_id', 'orders', 'order_id')}"),
        ("orders", "order_reviews", "1:N", f"order_id；涵蓋 {review_order_count:,} 張訂單；{coverage('order_reviews', 'order_id', 'orders', 'order_id')}；評論有重複列"),
        ("products", "order_items", "1:N", f"product_id；{coverage('order_items', 'product_id', 'products', 'product_id')}；有未被訂購商品"),
        ("sellers", "order_items", "1:N", f"seller_id；{coverage('order_items', 'seller_id', 'sellers', 'seller_id')}"),
        ("translation", "products", "1:N 部分", "product_category_name；有空分類及未翻譯分類"),
        ("geolocation", "customers", "M:N（原始列）", "郵遞區號兩側都不唯一；相同郵遞區號會多筆匹配"),
        ("geolocation", "sellers", "M:N（原始列）", "郵遞區號兩側都不唯一；相同郵遞區號會多筆匹配"),
    ]
    lines = ["## 3. 關聯", "", "| 來源 | 目標 | 實際基數 | 關聯欄位與檢查 |", "|---|---|---|---|"]
    lines.extend(f"| {source} | {target} | {cardinality} | {detail} |" for source, target, cardinality, detail in rows)
    lines.extend([
        "",
        "```mermaid",
        "erDiagram",
        "    CUSTOMERS ||--|| ORDERS : customer_id",
        "    ORDERS ||--o{ ORDER_ITEMS : order_id",
        "    ORDERS ||--o{ ORDER_PAYMENTS : order_id",
        "    ORDERS ||--o{ ORDER_REVIEWS : order_id",
        "    PRODUCTS ||--o{ ORDER_ITEMS : product_id",
        "    SELLERS ||--o{ ORDER_ITEMS : seller_id",
        "    TRANSLATION ||--o{ PRODUCTS : product_category_name",
        "    GEOLOCATION }o--o{ CUSTOMERS : zip_prefix",
        "    GEOLOCATION }o--o{ SELLERS : zip_prefix",
        "```",
        "",
        "郵遞區號關係以原始資料列計算；geolocation 的 `geolocation_zip_code_prefix` 不唯一，直接連接會擴增列數。",
        "",
    ])
    return lines


def main():
    tables = load_tables()
    lines = [
        "# 電商 CSV 資料剖析與品質檢查",
        "",
        "分析方式：以 pandas 讀取 CSV，不寫入或改動來源資料。欄位型態是讀取時推斷型態；日期目前在 CSV 中以字串讀入。",
        "",
    ]
    lines.extend(schema_section(tables))
    lines.extend(["## 2. 粒度與主鍵檢查", "", "| 資料表 | 粒度 | 候選主鍵 / 唯一性 |", "|---|---|---|"])
    for name, frame in tables.items():
        lines.append(f"| `{FILES[name]}` | {GRAINS[name]} | {key_summary(name, frame)} |")
    lines.append("")
    lines.extend(relationship_section(tables))
    issues = quality_issues(tables)
    lines.extend(["## 4. 資料品質問題", ""])
    if issues:
        lines.extend(["| 問題 | 筆數 | 判定方式 | 範例（原始欄位） |", "|---|---:|---|---|"])
        lines.extend(f"| {title} | {count:,} | {rule} | `{fmt_example(example)}` |" for title, count, rule, example in issues)
    else:
        lines.append("未偵測到所設定規則中的問題。")
    lines.extend([
        "",
        "## 補充說明",
        "",
        "- 評論標題/內容缺值及非已完成訂單的物流日期缺值已在欄位摘要列出；它們可能是選填或狀態所致，未一概判定為錯誤。",
        "- `customer_unique_id` 在客戶表有重複值，不能當作該表主鍵；本次資料中 `customer_id` 唯一。",
        "- 金額差異只表示明細與付款總額不一致，未假設折扣、退款或取貨付款的業務規則。",
        "",
    ])
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"已產生 {REPORT_PATH.name}")
    print(f"資料表：{len(tables)}；資料品質檢查項目：{len(issues)}")
    for title, count, _, _ in issues:
        print(f"- {title}: {count:,}")


if __name__ == "__main__":
    main()