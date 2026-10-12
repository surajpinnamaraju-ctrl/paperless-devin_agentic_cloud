import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any
from typing import TypeVar
from typing import cast

from django.db import OperationalError
from django.db import connections
from django.db import models
from django.db import router
from django.db import transaction

CONTENT_FIELDS = frozenset(
    {
        "content",
        "checksum",
        "archive_checksum",
        "mime_type",
        "page_count",
        "root_document_id",
        "version_index",
    },
)
_actor = ContextVar("document_review_actor", default=None)
_preserve = ContextVar("preserve_document_review", default=False)
F = TypeVar("F", bound=Callable[..., Any])


def retry_document_write(method: F) -> F:
    @wraps(method)
    def wrapped(instance: Any, *args: Any, **kwargs: Any) -> Any:
        using = (
            kwargs.get("using")
            or getattr(instance, "db", None)
            or router.db_for_write(type(instance), instance=instance)
        )
        connection = connections[using]
        if connection.vendor != "sqlite" or connection.in_atomic_block:
            return method(instance, *args, **kwargs)
        for attempt in range(8):
            try:
                return method(instance, *args, **kwargs)
            except OperationalError as exc:
                if not any(word in str(exc).lower() for word in ("locked", "busy")):
                    raise
                if attempt == 7:
                    raise
                time.sleep(min(0.01 * 2**attempt, 0.2))

    return cast("F", wrapped)


@contextmanager
def review_actor(actor):
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)


@contextmanager
def preserve_review():
    token = _preserve.set(True)
    try:
        yield
    finally:
        _preserve.reset(token)


def request_review_actor(func):
    @wraps(func)
    def wrapped(self, request, *args, **kwargs):
        with review_actor(request.user):
            return func(self, request, *args, **kwargs)

    return wrapped


def lock_document(document_id, *, using="default"):
    from documents.models import Document

    # UPDATE also acquires a write lock on SQLite, where FOR UPDATE is a no-op.
    qs = Document.global_objects.using(using).filter(pk=document_id)
    models.QuerySet.update(qs, id=models.F("id"))
    return qs.select_for_update().get()


def invalidate_locked(document):
    from documents.review import invalidate_review

    if not _preserve.get():
        invalidate_review(document, actor=_actor.get())


def save_document(document, save, args, kwargs):
    from documents.models import Document

    update_fields = kwargs.get("update_fields")
    fields = (
        {"root_document_id" if f == "root_document" else f for f in update_fields}
        if update_fields is not None
        else CONTENT_FIELDS - document.get_deferred_fields()
    )
    if not fields.intersection(CONTENT_FIELDS) or (
        document._state.adding
        and document.pk is None
        and document.root_document_id is None
    ):
        return save(*args, **kwargs)
    using = kwargs.get("using") or router.db_for_write(Document, instance=document)
    manager = Document.global_objects.using(using)
    previous_identity = (
        manager.filter(pk=document.pk).values("id", "root_document_id").first()
        if document.pk is not None
        else None
    )
    with transaction.atomic(using=using):
        previous_root = (
            previous_identity["root_document_id"] or previous_identity["id"]
            if previous_identity
            else None
        )
        root_ids = {
            root_id
            for root_id in (previous_root, document.root_document_id)
            if root_id is not None
        }
        roots = {
            root_id: lock_document(root_id, using=using) for root_id in sorted(root_ids)
        }
        previous = (
            manager.filter(pk=document.pk).values(*CONTENT_FIELDS).first()
            if document.pk is not None
            else None
        )
        result = save(*args, **kwargs)
        current = manager.filter(pk=document.pk).values(*CONTENT_FIELDS).get()
        if (previous is None and current["root_document_id"] is not None) or (
            previous is not None and previous != current
        ):
            for root in roots.values():
                invalidate_locked(root)
        return result
