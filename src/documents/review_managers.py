from typing import Any

from django.db import models
from django.db import transaction
from django_softdelete.managers import DeletedManager
from django_softdelete.managers import DeletedQuerySet
from django_softdelete.managers import GlobalManager
from django_softdelete.managers import GlobalQuerySet
from django_softdelete.managers import SoftDeleteManager
from django_softdelete.managers import SoftDeleteQuerySet

from documents.review_hooks import CONTENT_FIELDS
from documents.review_hooks import invalidate_locked
from documents.review_hooks import lock_document
from documents.review_hooks import retry_document_write


class ReviewContentQuerySetMixin(models.QuerySet):
    @retry_document_write
    def update(self, **kwargs: Any) -> int:
        fields = {"root_document_id" if f == "root_document" else f for f in kwargs}
        if not fields.intersection(CONTENT_FIELDS):
            return super().update(**kwargs)

        with transaction.atomic(using=self.db):
            # Materialize only IDs; full content snapshots are bounded to each batch.
            ids = list(self.order_by("pk").values_list("pk", flat=True))
            count = 0
            for offset in range(0, len(ids), 100):
                batch = self.model.global_objects.using(self.db).filter(
                    pk__in=ids[offset : offset + 100],
                )
                root_ids = set(
                    batch.annotate(
                        review_root=models.functions.Coalesce("root_document_id", "id"),
                    ).values_list("review_root", flat=True),
                )
                new_root = kwargs.get("root_document_id", kwargs.get("root_document"))
                if new_root is not None:
                    if isinstance(new_root, models.Model):
                        root_ids.add(new_root.pk)
                    elif hasattr(new_root, "resolve_expression"):
                        root_ids.update(
                            batch.annotate(review_new_root=new_root).values_list(
                                "review_new_root",
                                flat=True,
                            ),
                        )
                    else:
                        root_ids.add(new_root)
                roots = {
                    root_id: lock_document(root_id, using=self.db)
                    for root_id in sorted(root_ids - {None})
                }
                before = {row["id"]: row for row in batch.values("id", *CONTENT_FIELDS)}
                count += models.QuerySet.update(batch, **kwargs)
                changed_roots = set()
                for row in batch.values("id", *CONTENT_FIELDS):
                    old = before[row["id"]]
                    if row != old:
                        changed_roots.add(old["root_document_id"] or old["id"])
                        changed_roots.add(row["root_document_id"] or row["id"])
                for root_id in sorted(changed_roots):
                    invalidate_locked(roots[root_id])
            self._result_cache = None
            return count

    @retry_document_write
    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        root_ids = {obj.root_document_id for obj in objs} - {None}
        if not root_ids:
            return super().bulk_create(objs, *args, **kwargs)
        with transaction.atomic(using=self.db):
            roots = {
                root_id: lock_document(root_id, using=self.db)
                for root_id in sorted(root_ids)
            }
            before = {
                root_id: self.model.global_objects.using(self.db)
                .filter(
                    root_document_id=root_id,
                )
                .count()
                for root_id in root_ids
            }
            result = super().bulk_create(objs, *args, **kwargs)
            for root_id, root in roots.items():
                if (
                    self.model.global_objects.using(self.db)
                    .filter(
                        root_document_id=root_id,
                    )
                    .count()
                    != before[root_id]
                ):
                    invalidate_locked(root)
            return result


class DocumentQuerySet(ReviewContentQuerySetMixin, SoftDeleteQuerySet):
    pass


class DeletedDocumentQuerySet(ReviewContentQuerySetMixin, DeletedQuerySet):
    pass


class GlobalDocumentQuerySet(ReviewContentQuerySetMixin, GlobalQuerySet):
    pass


class DocumentManager(SoftDeleteManager):
    def get_queryset(self) -> DocumentQuerySet:
        return DocumentQuerySet(
            self.model,
            using=self._db,
            hints=getattr(self, "_hints", {}),
        ).filter(deleted_at__isnull=True)


class DeletedDocumentManager(DeletedManager):
    def get_queryset(self) -> DeletedDocumentQuerySet:
        return DeletedDocumentQuerySet(
            self.model,
            using=self._db,
            hints=getattr(self, "_hints", {}),
        ).filter(deleted_at__isnull=False)


class GlobalDocumentManager(GlobalManager):
    def get_queryset(self) -> GlobalDocumentQuerySet:
        return GlobalDocumentQuerySet(
            self.model,
            using=self._db,
            hints=getattr(self, "_hints", {}),
        )
