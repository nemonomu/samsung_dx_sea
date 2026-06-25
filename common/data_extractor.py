"""
DataExtractor - 데이터 추출 유틸리티

크롤링된 텍스트에서 필요한 데이터를 추출하고 변환하는 유틸리티 함수 모음

주요 기능:
- 숫자 추출: 텍스트에서 가격, 별점, 리뷰 개수 등의 숫자 추출
- 데이터 변환: 추출된 값을 표준 형식으로 변환
- 별점 분포 계산: HTML에서 별점 통계 추출 및 포맷팅

사용법:
    from common import data_extractor

    price = data_extractor.extract_numeric_value("$1,234.56")  # "1,234.56"
    no_reviews = data_extractor.get_no_reviews_text("Amazon")  # "No customer reviews"
    text = data_extractor.extract_text_before_or_after("Hello World", "World", "before")  # "Hello"

모든 크롤러에서 독립적으로 import하여 사용 가능
"""

import re


def extract_numeric_value(text, include_comma=True, include_decimal=True):
    """
    텍스트에서 숫자 추출 (쉼표, 소수점 옵션 가능)

    Args:
        text (str): 원본 텍스트
        include_comma (bool): 쉼표 포함 여부 (기본값: True)
        include_decimal (bool): 소수점 포함 여부 (기본값: True)

    Returns:
        str: 숫자만 추출된 문자열 또는 None

    Examples:
        - extract_numeric_value("$1,234.56") → "1,234.56"
        - extract_numeric_value("3,572등급", include_decimal=False) → "3,572"
        - extract_numeric_value("4.5 out of 5", include_comma=False) → "4.5"
    """
    if not text:
        return None

    if include_comma and include_decimal:
        pattern = r'[\d,.]+'
    elif include_comma:
        pattern = r'[\d,]+'
    elif include_decimal:
        pattern = r'\d+\.?\d*'
    else:
        pattern = r'\d+'

    match = re.search(pattern, text)
    return match.group(0) if match else None


def get_no_reviews_text(account_name):
    """
    쇼핑몰별 리뷰 없음 텍스트 반환

    Args:
        account_name (str): 쇼핑몰 계정명 (예: "Amazon", "BestBuy", "Walmart")

    Returns:
        str: 쇼핑몰별 리뷰 없음 텍스트

    Examples:
        - get_no_reviews_text("Amazon") → "No customer reviews"
        - get_no_reviews_text("Bestbuy") → "Not yet reviewed"
        - get_no_reviews_text("Walmart") → "No ratings yet"
    """
    no_reviews_mapping = {
        'Amazon': 'No customer reviews',
        'Bestbuy': 'Not yet reviewed',
        'Walmart': 'No ratings yet'
    }

    return no_reviews_mapping.get(account_name, 'No reviews')


def extract_text_before_or_after(raw_text, cut_text, position='before'):
    """
    특정 텍스트 기준으로 앞 또는 뒤 텍스트 추출

    쓰임새:
    - Prime 메시지에서 'Join Prime' 이전 텍스트만 추출
    - 특정 구분자 기준으로 텍스트 분리

    Args:
        raw_text (str): 원본 텍스트
        cut_text (str): 자를 기준 텍스트
        position (str): 'before' (앞) 또는 'after' (뒤)

    Returns:
        str or None: 추출된 텍스트 또는 None

    Examples:
        - extract_text_before_or_after("Or Prime members get FREE delivery Saturday. Join Prime", "Join Prime", "before")
          → "Or Prime members get FREE delivery Saturday."
        - extract_text_before_or_after("Price: $999 - Details", " - ", "before")
          → "Price: $999"
        - extract_text_before_or_after("Category: Electronics", ": ", "after")
          → "Electronics"
    """
    if not raw_text:
        return None

    if not cut_text or cut_text not in raw_text:
        return raw_text.strip() if raw_text else None

    if position == 'before':
        return raw_text.split(cut_text)[0].strip()
    elif position == 'after':
        parts = raw_text.split(cut_text, 1)
        return parts[1].strip() if len(parts) > 1 else None
    else:
        return raw_text.strip()