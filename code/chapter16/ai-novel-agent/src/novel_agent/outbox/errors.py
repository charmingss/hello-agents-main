class OutboxConflictError(Exception):
    """An event identity was reused with a different immutable envelope or payload."""


__all__ = ["OutboxConflictError"]
