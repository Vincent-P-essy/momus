import re


def slugify(text):
    """Return a URL-safe slug: lowercase, hyphen-separated ASCII."""
    text = text.strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
