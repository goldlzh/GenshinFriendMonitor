"""User-facing changes; diagnostic observations remain in the database."""

STATUS_CHANGE_LABELS = {
    'online': '上线',
    'offline': '下线',
    'added': '新增',
    'removed': '消失/可能被删',
}


def has_event_identity(row):
    """A screenshot/local row number alone cannot identify a change to users."""
    return any(str(row.get(key) or '').strip() for key in ('uid', 'nickname'))


def is_status_change(row):
    return row.get('event_type') in STATUS_CHANGE_LABELS and has_event_identity(row)
