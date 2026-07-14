def add_tag(item, tag, tags=None):
    if tags is None:
        tags = []
    tags.append((item, tag))
    return tags
