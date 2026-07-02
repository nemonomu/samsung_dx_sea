import json
import os

from .step00_config import bestbuy_output_table, bestbuy_product_list_table, db_config


TARGET_SCHEMA = "public"
DRY_RUN = os.getenv("BESTBUY_DB_LOAD_DRY_RUN", "0").lower() in {"1", "true", "yes", "y"}
ADD_MISSING_COLUMNS = os.getenv("BESTBUY_DB_PREPARE_ADD_MISSING_COLUMNS", "1").lower() in {
    "1",
    "true",
    "yes",
    "y",
}


COMMON_COLUMNS = [
    ("id", "serial PRIMARY KEY"),
    ("product", "varchar"),
    ("item", "varchar"),
    ("account_name", "varchar"),
    ("page_type", "varchar"),
    ("count_of_reviews", "text"),
    ("retailer_sku_name", "text"),
    ("product_url", "text"),
    ("star_rating", "varchar"),
    ("count_of_star_ratings", "text"),
    ("sku_popularity", "varchar"),
    ("final_sku_price", "varchar"),
    ("original_sku_price", "varchar"),
    ("savings", "varchar"),
    ("discount_type", "varchar"),
    ("offer", "text"),
    ("pick_up_availability", "text"),
    ("fastest_delivery", "text"),
    ("delivery_availability", "text"),
    ("available_quantity_for_purchase", "varchar"),
    ("inventory_status", "varchar"),
    ("sku_status", "varchar"),
    ("retailer_membership_discounts", "varchar"),
    ("detailed_review_content", "text"),
    ("recommendation_intent", "varchar"),
    ("main_rank", "integer"),
    ("bsr_rank", "integer"),
    ("rank_1", "varchar"),
    ("rank_2", "varchar"),
    ("trend_rank", "integer"),
    ("number_of_ppl_purchased_yesterday", "integer"),
    ("number_of_ppl_added_to_carts", "integer"),
    ("number_of_units_purchased_past_month", "integer"),
    ("retailer_sku_name_similar", "text"),
    ("promotion_type", "varchar"),
    ("calendar_week", "varchar"),
    ("crawl_datetime", "varchar"),
    ("batch_id", "varchar"),
    ("country", "varchar"),
]


HHP_COLUMNS = [
    ("id", "serial PRIMARY KEY"),
    ("country", "varchar(10) NOT NULL DEFAULT 'SEA'"),
    ("product", "varchar(20) NOT NULL DEFAULT 'HHP'"),
    ("item", "text"),
    ("account_name", "varchar(50) NOT NULL DEFAULT 'Bestbuy'"),
    ("page_type", "varchar(50)"),
    ("count_of_reviews", "text"),
    ("retailer_sku_name", "text"),
    ("product_url", "text"),
    ("star_rating", "text"),
    ("count_of_star_ratings", "text"),
    ("final_sku_price", "text"),
    ("original_sku_price", "text"),
    ("savings", "text"),
    ("offer", "text"),
    ("pick_up_availability", "text"),
    ("fastest_delivery", "text"),
    ("sku_status", "text"),
    ("trade_in", "text"),
    ("hhp_storage", "text"),
    ("hhp_color", "text"),
    ("hhp_carrier", "text"),
    ("detailed_review_content", "text"),
    ("recommendation_intent", "text"),
    ("main_rank", "integer"),
    ("bsr_rank", "integer"),
    ("trend_rank", "integer"),
    ("retailer_sku_name_similar", "text"),
    ("promotion_type", "text"),
    ("calendar_week", "varchar(20)"),
    ("crawl_strdatetime", "varchar(50)"),
    ("batch_id", "text"),
]


REF_COLUMNS = [
    ("id", "serial PRIMARY KEY"),
    ("country", "varchar(10) NOT NULL DEFAULT 'SEA'"),
    ("product", "varchar(20) NOT NULL DEFAULT 'REF'"),
    ("item", "text"),
    ("sku", "text"),
    ("account_name", "varchar(50) NOT NULL DEFAULT 'Bestbuy'"),
    ("page_type", "varchar(50)"),
    ("count_of_reviews", "text"),
    ("retailer_sku_name", "text"),
    ("product_url", "text"),
    ("star_rating", "text"),
    ("count_of_star_ratings", "text"),
    ("final_sku_price", "text"),
    ("original_sku_price", "text"),
    ("savings", "text"),
    ("offer", "text"),
    ("pick_up_availability", "text"),
    ("delivery_availability", "text"),
    ("sku_status", "text"),
    ("detailed_review_content", "text"),
    ("recommendation_intent", "text"),
    ("main_rank", "integer"),
    ("bsr_rank", "integer"),
    ("retailer_sku_name_similar", "text"),
    ("ref_capacity", "text"),
    ("ref_refrigerator_type", "text"),
    ("calendar_week", "varchar(20)"),
    ("crawl_strdatetime", "varchar(50)"),
    ("batch_id", "text"),
]


LDY_COLUMNS = [
    ("id", "serial PRIMARY KEY"),
    ("country", "varchar(10) NOT NULL DEFAULT 'SEA'"),
    ("product", "varchar(20) NOT NULL DEFAULT 'LDY'"),
    ("item", "text"),
    ("sku", "text"),
    ("account_name", "varchar(50) NOT NULL DEFAULT 'Bestbuy'"),
    ("page_type", "varchar(50)"),
    ("count_of_reviews", "text"),
    ("retailer_sku_name", "text"),
    ("product_url", "text"),
    ("star_rating", "text"),
    ("count_of_star_ratings", "text"),
    ("final_sku_price", "text"),
    ("original_sku_price", "text"),
    ("savings", "text"),
    ("offer", "text"),
    ("pick_up_availability", "text"),
    ("delivery_availability", "text"),
    ("sku_status", "text"),
    ("detailed_review_content", "text"),
    ("recommendation_intent", "text"),
    ("main_rank", "integer"),
    ("bsr_rank", "integer"),
    ("retailer_sku_name_similar", "text"),
    ("ldy_capacity", "text"),
    ("ldy_loading_type", "text"),
    ("calendar_week", "varchar(20)"),
    ("crawl_strdatetime", "varchar(50)"),
    ("batch_id", "text"),
]


SCHEMAS = {
    "HHP": HHP_COLUMNS,
    "REF": REF_COLUMNS,
    "LDY": LDY_COLUMNS,
}


TV_PRODUCT_LIST_COLUMNS = [
    ("id", "serial4 NOT NULL PRIMARY KEY"),
    ("account_name", "varchar(50) NOT NULL"),
    ("page_type", "varchar(50) NOT NULL"),
    ("retailer_sku_name", "text NULL"),
    ("final_sku_price", "varchar(50) NULL"),
    ("savings", "varchar(50) NULL"),
    ("comparable_pricing", "varchar(100) NULL"),
    ("offer", "varchar(100) NULL"),
    ("pick_up_availability", "varchar(100) NULL"),
    ("fastest_delivery", "varchar(100) NULL"),
    ("delivery_availability", "varchar(100) NULL"),
    ("sku_status", "varchar(100) NULL"),
    ("promotion_type", "varchar(100) NULL"),
    ("trend_rank", "int4 NULL"),
    ("main_rank", "int4 NULL"),
    ("bsr_rank", "int4 NULL"),
    ("product_url", "text NULL"),
    ("calendar_week", "varchar(10) NULL"),
    ("crawl_datetime", "varchar(50) NULL"),
    ("batch_id", "varchar(50) NULL"),
    ("main_page_number", "int4 NULL"),
    ("bsr_page_number", "int4 NULL"),
    ("promotion_position", "int4 NULL"),
    ("category_key", "varchar(20) NULL"),
    ("final_target_rank", "int4 NULL"),
    ("sku_id", "varchar(50) NULL"),
]


HHP_PRODUCT_LIST_COLUMNS = [
    ("id", "serial4 NOT NULL PRIMARY KEY"),
    ("account_name", "varchar(50) NOT NULL"),
    ("page_type", "varchar(50) NOT NULL"),
    ("retailer_sku_name", "text NULL"),
    ("final_sku_price", "varchar(50) NULL"),
    ("savings", "varchar(50) NULL"),
    ("comparable_pricing", "varchar(100) NULL"),
    ("offer", "varchar(100) NULL"),
    ("pick_up_availability", "varchar(100) NULL"),
    ("fastest_delivery", "varchar(100) NULL"),
    ("sku_status", "varchar(100) NULL"),
    ("promotion_type", "varchar(100) NULL"),
    ("trend_rank", "int4 NULL"),
    ("main_rank", "int4 NULL"),
    ("bsr_rank", "int4 NULL"),
    ("product_url", "text NULL"),
    ("calendar_week", "varchar(10) NULL"),
    ("crawl_strdatetime", "varchar(50) NULL"),
    ("batch_id", "varchar(50) NULL"),
    ("main_page_number", "int4 NULL"),
    ("bsr_page_number", "int4 NULL"),
    ("category_key", "varchar(20) NULL"),
    ("final_target_rank", "int4 NULL"),
    ("sku_id", "varchar(50) NULL"),
]

REF_LDY_PRODUCT_LIST_COLUMNS = [
    ("id", "serial4 NOT NULL PRIMARY KEY"),
    ("account_name", "varchar(50) NOT NULL"),
    ("page_type", "varchar(50) NOT NULL"),
    ("retailer_sku_name", "text NULL"),
    ("final_sku_price", "varchar(50) NULL"),
    ("savings", "varchar(50) NULL"),
    ("comparable_pricing", "varchar(100) NULL"),
    ("offer", "varchar(100) NULL"),
    ("pick_up_availability", "varchar(100) NULL"),
    ("fastest_delivery", "varchar(100) NULL"),
    ("delivery_availability", "varchar(100) NULL"),
    ("sku_status", "varchar(100) NULL"),
    ("promotion_type", "varchar(100) NULL"),
    ("trend_rank", "int4 NULL"),
    ("main_rank", "int4 NULL"),
    ("bsr_rank", "int4 NULL"),
    ("product_url", "text NULL"),
    ("calendar_week", "varchar(10) NULL"),
    ("crawl_strdatetime", "varchar(50) NULL"),
    ("batch_id", "varchar(50) NULL"),
    ("main_page_number", "int4 NULL"),
    ("bsr_page_number", "int4 NULL"),
    ("category_key", "varchar(20) NULL"),
    ("final_target_rank", "int4 NULL"),
    ("sku_id", "varchar(50) NULL"),
]

PRODUCT_LIST_SCHEMAS = {
    "TV": TV_PRODUCT_LIST_COLUMNS,
    "HHP": HHP_PRODUCT_LIST_COLUMNS,
    "REF": REF_LDY_PRODUCT_LIST_COLUMNS,
    "LDY": REF_LDY_PRODUCT_LIST_COLUMNS,
}


def quote_ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def table_exists(cur, table_name, schema=TARGET_SCHEMA):
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        )
        """,
        (schema, table_name),
    )
    return bool(cur.fetchone()[0])


def existing_column_names(cur, table_name, schema=TARGET_SCHEMA):
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table_name),
    )
    return {row[0] for row in cur.fetchall()}


def create_table(cur, table_name, columns):
    column_sql = ",\n  ".join(f"{quote_ident(name)} {data_type}" for name, data_type in columns)
    cur.execute(f"CREATE TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} (\n  {column_sql}\n)")


def add_missing_columns(cur, table_name, columns):
    existing = existing_column_names(cur, table_name)
    missing = [(name, data_type) for name, data_type in columns if name not in existing]
    if not missing:
        return []
    if any(name == "id" for name, _ in missing):
        raise RuntimeError(f"Existing table is missing required id column: {TARGET_SCHEMA}.{table_name}")
    if not ADD_MISSING_COLUMNS:
        return missing
    for name, data_type in missing:
        cur.execute(
            f"ALTER TABLE {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} "
            f"ADD COLUMN {quote_ident(name)} {data_type}"
        )
    return missing


def create_product_list_indexes(cur, table_name, crawl_column):
    prefix = table_name[:45]
    cur.execute(
        f"CREATE INDEX {quote_ident(f'idx_{prefix}_crawl')} "
        f"ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} USING btree ({quote_ident(crawl_column)})"
    )
    cur.execute(
        f"CREATE INDEX {quote_ident(f'idx_{prefix}_page_type')} "
        f"ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} USING btree (page_type)"
    )
    cur.execute(
        f"CREATE INDEX {quote_ident(f'idx_{prefix}_product_url')} "
        f"ON {quote_ident(TARGET_SCHEMA)}.{quote_ident(table_name)} USING btree (product_url)"
    )


def main():
    import psycopg2

    config = db_config()
    if not config:
        if DRY_RUN:
            print(json.dumps({"dry_run": True, "skipped": True, "reason": "DB_CONFIG is missing"}))
            return
        raise RuntimeError("DB_CONFIG is missing")

    conn = psycopg2.connect(
        host=config.get("host"),
        port=int(config.get("port") or 5432),
        user=config.get("user"),
        password=config.get("password"),
        dbname=config.get("database"),
        connect_timeout=10,
    )
    created = []
    skipped = []
    altered = []
    missing_not_added = []
    with conn:
        with conn.cursor() as cur:
            for category, columns in SCHEMAS.items():
                table_name = bestbuy_output_table(category)
                if table_exists(cur, table_name):
                    missing = add_missing_columns(cur, table_name, columns)
                    if missing and ADD_MISSING_COLUMNS:
                        altered.append({"table": table_name, "columns": [name for name, _ in missing]})
                    elif missing:
                        missing_not_added.append({"table": table_name, "columns": [name for name, _ in missing]})
                    skipped.append(table_name)
                    continue
                create_table(cur, table_name, columns)
                created.append(table_name)
            for category, columns in PRODUCT_LIST_SCHEMAS.items():
                table_name = bestbuy_product_list_table(category)
                if table_exists(cur, table_name):
                    missing = add_missing_columns(cur, table_name, columns)
                    if missing and ADD_MISSING_COLUMNS:
                        altered.append({"table": table_name, "columns": [name for name, _ in missing]})
                    elif missing:
                        missing_not_added.append({"table": table_name, "columns": [name for name, _ in missing]})
                    skipped.append(table_name)
                    continue
                create_table(cur, table_name, columns)
                crawl_column = "crawl_datetime" if category == "TV" else "crawl_strdatetime"
                create_product_list_indexes(cur, table_name, crawl_column)
                created.append(table_name)
    conn.close()
    print(
        {
            "created": created,
            "skipped_existing": skipped,
            "altered_existing": altered,
            "missing_not_added": missing_not_added,
            "add_missing_columns": ADD_MISSING_COLUMNS,
        }
    )


if __name__ == "__main__":
    main()
