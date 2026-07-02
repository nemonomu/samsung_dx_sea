ALL_AVAILABILITY_FIELDS = ["pick_up_availability", "fastest_delivery", "delivery_availability"]

CATEGORY_AVAILABILITY_FIELDS = {
    "TV": ["pick_up_availability", "fastest_delivery", "delivery_availability"],
    "HHP": ["pick_up_availability", "fastest_delivery"],
    "LDY": ["pick_up_availability", "delivery_availability"],
    "REF": ["pick_up_availability", "delivery_availability"],
}


def active_availability_fields(category):
    key = str(category or "").strip().upper()
    return list(CATEGORY_AVAILABILITY_FIELDS.get(key, ALL_AVAILABILITY_FIELDS))


def inactive_availability_fields(category):
    active = set(active_availability_fields(category))
    return [field for field in ALL_AVAILABILITY_FIELDS if field not in active]
