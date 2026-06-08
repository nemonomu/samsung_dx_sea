-- Create test TV retail data table.
-- Purpose:
--   - Shared/unified TV retail table shape.
--   - Walmart TV crawler inserts only its current DB-shape columns.
--   - Other shared crawler columns remain nullable.

CREATE TABLE IF NOT EXISTS public.test_tv_retail_com (
    id SERIAL PRIMARY KEY,
    item VARCHAR(255),
    account_name VARCHAR(255),
    page_type VARCHAR(50),
    count_of_reviews TEXT,
    retailer_sku_name TEXT,
    product_url TEXT,
    star_rating VARCHAR(20),
    count_of_star_ratings TEXT,
    screen_size VARCHAR(50),
    sku_popularity VARCHAR(100),
    final_sku_price VARCHAR(50),
    original_sku_price VARCHAR(50),
    savings VARCHAR(50),
    discount_type VARCHAR(100),
    offer TEXT,
    pick_up_availability VARCHAR(50),
    fastest_delivery TEXT,
    delivery_availability TEXT,
    shipping_info TEXT,
    available_quantity_for_purchase VARCHAR(50),
    inventory_status VARCHAR(100),
    sku_status VARCHAR(100),
    retailer_membership_discounts VARCHAR(100),
    detailed_review_content TEXT,
    summarized_review_content TEXT,
    top_mentions TEXT,
    recommendation_intent VARCHAR(100),
    main_rank INTEGER,
    bsr_rank INTEGER,
    rank_1 VARCHAR(255),
    rank_2 VARCHAR(255),
    promotion_position INTEGER,
    trend_rank INTEGER,
    number_of_ppl_purchased_yesterday INTEGER,
    number_of_ppl_added_to_carts INTEGER,
    retailer_sku_name_similar TEXT,
    estimated_annual_electricity_use VARCHAR(50),
    promotion_type VARCHAR(100),
    calendar_week VARCHAR(20),
    crawl_datetime VARCHAR(50),
    number_of_units_purchased_past_month INTEGER,
    model_year VARCHAR(10),
    batch_id VARCHAR(50),
    country VARCHAR(50),
    redirect BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_test_tv_retail_com_item
    ON public.test_tv_retail_com(item);

CREATE INDEX IF NOT EXISTS idx_test_tv_retail_com_account
    ON public.test_tv_retail_com(account_name);

CREATE INDEX IF NOT EXISTS idx_test_tv_retail_com_page_type
    ON public.test_tv_retail_com(page_type);

CREATE INDEX IF NOT EXISTS idx_test_tv_retail_com_crawl_datetime
    ON public.test_tv_retail_com(crawl_datetime);

CREATE INDEX IF NOT EXISTS idx_test_tv_retail_com_calendar_week
    ON public.test_tv_retail_com(calendar_week);

CREATE INDEX IF NOT EXISTS idx_test_tv_retail_com_batch_id
    ON public.test_tv_retail_com(batch_id);

COMMENT ON TABLE public.test_tv_retail_com IS
    'Test unified TV retail table. Walmart TV crawler inserts its DB-shape columns; other shared crawler columns are nullable.';
