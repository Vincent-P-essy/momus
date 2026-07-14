def paginate(items, page, per_page):
    start = page * per_page
    return items[start : start + per_page]
