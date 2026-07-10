import os

from . import step01_main_list


def main():
    os.environ.setdefault("BESTBUY_MAIN_PAGES", "6")
    os.environ.setdefault("BESTBUY_MAIN_RUN_ID", "bsr")
    os.environ.setdefault("BESTBUY_MAIN_ORGANIC_OFFSET", "18")
    os.environ.setdefault("BESTBUY_SEARCH_SORT", "Best-Selling")
    # Combo/bundle docs dilute best-seller pages, so page dynamically until 100
    # organic ranks are collected instead of a fixed page count (see step01).
    os.environ.setdefault("BESTBUY_LISTING_ORGANIC_TARGET", "100")
    os.environ.setdefault("BESTBUY_LISTING_MAX_PAGES", "20")
    os.environ.setdefault("BESTBUY_LISTING_PAGE_COMPLETE_MIN_ROWS", "8")
    step01_main_list.main()


if __name__ == "__main__":
    main()
