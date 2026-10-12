"""Document review operations, serialized with content writes on the root row."""

import time
import uuid

from django.contrib.auth.models import User
from django.db import OperationalError
from django.db import connections
from django.db import transaction
from django.db.models import F
from django.db.models import Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from rest_framework.exceptions import APIException
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError

from documents.models import Document
from documents.models import DocumentReview
from documents.models import ReviewEvent
from documents.models import ReviewRequest
from documents.permissions import has_perms_owner_aware
from documents.permissions import permitted_document_ids


class ReviewConflict(APIException):
    status_code = 409
    default_detail = "Review changed. Refresh and try again."


def can_access(user, document, action="view"):
    return (
        user.is_active
        and user.has_perm(f"documents.{action}_document")
        and (
            user.is_superuser
            or has_perms_owner_aware(user, f"{action}_document", document)
        )
    )


def _visible_document(user, document_id):
    try:
        document = Document.global_objects.get(pk=document_id)
        if document.root_document_id:
            document = Document.global_objects.get(pk=document.root_document_id)
    except Document.DoesNotExist as exc:
        raise NotFound from exc
    if not can_access(user, document):
        raise NotFound
    return document


def visible_document(user, document_id):
    return retry_sqlite(lambda: _visible_document(user, document_id), "default")


def eligible_reviewers(document, actor):
    if document.deleted_at or not can_access(actor, document, "change"):
        return []
    users = User.objects.filter(is_active=True).exclude(pk=actor.pk)
    for codename in ("view_document", "change_document"):
        users = users.filter(
            Q(is_superuser=True)
            | Q(
                user_permissions__content_type__app_label="documents",
                user_permissions__codename=codename,
            )
            | Q(
                groups__permissions__content_type__app_label="documents",
                groups__permissions__codename=codename,
            ),
        )
    return [
        {"id": user.pk, "username": user.username}
        for user in users.distinct().order_by("pk")
        if can_access(user, document) and can_access(user, document, "change")
    ]


def review_response(document, actor, review=None):
    if review is None:
        review = (
            DocumentReview.objects.using(document._state.db)
            .filter(document=document)
            .first()
        )
    state = review.state if review else DocumentReview.State.UNREVIEWED
    allowed_actions = []
    if not document.deleted_at and can_access(actor, document, "change"):
        if state == DocumentReview.State.UNREVIEWED:
            allowed_actions = ["submit"]
        elif state in (DocumentReview.State.APPROVED, DocumentReview.State.REJECTED):
            allowed_actions = ["reopen"]
        elif (
            review
            and review.reviewer_id == actor.pk
            and review.submitted_by_id != actor.pk
        ):
            allowed_actions = ["approve", "reject"]
    return {
        "document": document.pk,
        "state": state,
        "revision": review.revision if review else 0,
        "reviewer": review.reviewer_id if review else None,
        "submitted_by": review.submitted_by_id if review else None,
        "history": [
            {
                "action": event.action,
                "actor": event.actor_id,
                "revision": event.revision,
                "at": event.at.isoformat().replace("+00:00", "Z"),
                "request_id": str(event.request_id) if event.request_id else None,
                "comment": event.comment,
            }
            for event in review.events.using(document._state.db).order_by("revision")
        ]
        if review
        else [],
        "allowed_actions": allowed_actions,
        "eligible_reviewers": eligible_reviewers(document, actor),
    }


def read_review(document, actor):
    using = document._state.db

    def read():
        with transaction.atomic(using=using):
            current = (
                Document.global_objects.using(using)
                .select_for_update()
                .get(pk=document.pk)
            )
            user = User.objects.using(using).get(pk=actor.pk)
            if not can_access(user, current):
                raise NotFound
            return review_response(current, user)

    return retry_sqlite(read, using)


def validate_payload(payload, document, actor):
    if not isinstance(payload, dict):
        raise ValidationError("Expected an object.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in (
        "submit",
        "approve",
        "reject",
        "reopen",
    ):
        raise ValidationError("Invalid action.")
    required = {"action", "expected_revision", "request_id"}
    if action == "submit":
        required.add("reviewer")
    if action == "reject":
        required.add("comment")
    if set(payload) != required:
        raise ValidationError("Missing or unknown fields.")
    revision = payload["expected_revision"]
    if type(revision) is not int or revision < 0:
        raise ValidationError("Invalid expected_revision.")
    if not isinstance(payload["request_id"], str):
        raise ValidationError("Invalid request_id.")
    try:
        uuid.UUID(payload["request_id"])
    except (ValueError, AttributeError) as exc:
        raise ValidationError("Invalid request_id.") from exc
    if action == "reject":
        comment = payload["comment"]
        if not isinstance(comment, str) or not 1 <= len(comment.strip()) <= 2000:
            raise ValidationError("Invalid comment.")
    if action == "submit":
        reviewer_id = payload["reviewer"]
        if type(reviewer_id) is not int or reviewer_id <= 0:
            raise ValidationError("Invalid reviewer.")


def validate_reviewer(payload, document, actor):
    if payload["action"] == "submit":
        reviewer = (
            User.objects.using(document._state.db)
            .filter(pk=payload["reviewer"], is_active=True)
            .first()
        )
        if (
            reviewer is None
            or reviewer.pk == actor.pk
            or not can_access(reviewer, document)
            or not can_access(reviewer, document, "change")
        ):
            previous = (
                ReviewRequest.objects.using(document._state.db)
                .filter(
                    document=document,
                    actor=actor,
                    request_id=payload["request_id"],
                )
                .first()
            )
            if previous and previous.payload == payload:
                return
            raise ValidationError("Invalid reviewer.")


def _mutate_review(document_id, actor_id, payload, using):
    with transaction.atomic(using=using):
        # Also obtains a SQLite write lock before creating a read snapshot.
        Document.global_objects.using(using).filter(pk=document_id).update(id=F("id"))
        document = (
            Document.global_objects.using(using).select_for_update().get(pk=document_id)
        )
        actor = User.objects.using(using).get(pk=actor_id)
        if not can_access(actor, document):
            raise NotFound
        validate_payload(payload, document, actor)
        validate_reviewer(payload, document, actor)
        if not can_access(actor, document, "change") or document.deleted_at:
            raise PermissionDenied
        review = DocumentReview.objects.using(using).filter(document=document).first()
        revision = review.revision if review else 0
        state = review.state if review else DocumentReview.State.UNREVIEWED
        action = payload["action"]
        previous = (
            ReviewRequest.objects.using(using)
            .filter(
                document=document,
                actor=actor,
                request_id=payload["request_id"],
            )
            .first()
        )
        if action in ("approve", "reject") and (
            not review
            or review.reviewer_id != actor.pk
            or review.submitted_by_id == actor.pk
        ):
            successful_retry = previous and previous.payload == payload
            stale_assignment = payload["expected_revision"] != revision and (
                ReviewRequest.objects.using(using)
                .filter(
                    document=document,
                    response__state=DocumentReview.State.PENDING,
                    response__reviewer=actor.pk,
                    response__revision=payload["expected_revision"],
                )
                .exclude(response__submitted_by=actor.pk)
                .exists()
            )
            if not successful_retry and not stale_assignment:
                raise PermissionDenied
        if previous:
            if previous.payload != payload:
                raise ReviewConflict(
                    "request_id was already used for a different payload.",
                )
            return previous.response
        if payload["expected_revision"] != revision:
            raise ReviewConflict
        transitions = {
            "submit": ({DocumentReview.State.UNREVIEWED}, DocumentReview.State.PENDING),
            "approve": ({DocumentReview.State.PENDING}, DocumentReview.State.APPROVED),
            "reject": ({DocumentReview.State.PENDING}, DocumentReview.State.REJECTED),
            "reopen": (
                {DocumentReview.State.APPROVED, DocumentReview.State.REJECTED},
                DocumentReview.State.UNREVIEWED,
            ),
        }
        sources, target = transitions[action]
        if state not in sources:
            raise ReviewConflict("Invalid review transition.")
        if review is None:
            review = DocumentReview(document=document)
        review.state = target
        review.revision = revision + 1
        if action == "submit":
            review.reviewer_id = payload["reviewer"]
            review.submitted_by = actor
        elif action == "reopen":
            review.reviewer = None
            review.submitted_by = None
        review.save(using=using)
        ReviewEvent.objects.using(using).create(
            review=review,
            action=action,
            actor=actor,
            revision=review.revision,
            request_id=payload["request_id"],
            comment=payload.get("comment", "").strip(),
        )
        response = review_response(document, actor, review)
        ReviewRequest.objects.using(using).create(
            document=document,
            actor=actor,
            request_id=payload["request_id"],
            payload=payload,
            response=response,
        )
        return response


def mutate_review(document, actor, payload):
    using = document._state.db
    return retry_sqlite(
        lambda: _mutate_review(document.pk, actor.pk, payload, using),
        using,
    )


def retry_sqlite(operation, using):
    nested = connections[using].in_atomic_block
    for attempt in range(8):
        try:
            return operation()
        except Document.DoesNotExist as exc:
            raise NotFound from exc
        except User.DoesNotExist as exc:
            raise PermissionDenied from exc
        except OperationalError as exc:
            if connections[using].vendor != "sqlite" or not any(
                word in str(exc).lower() for word in ("locked", "busy")
            ):
                raise
            if nested or attempt == 7:
                raise ReviewConflict("Document is busy. Retry the request.") from exc
            time.sleep(min(0.01 * 2**attempt, 0.2))


def invalidate_review(document, actor=None):
    """Call within the transaction holding the root Document's write lock."""
    using = document._state.db
    review = DocumentReview.objects.using(using).filter(document=document).first()
    if review is None or review.state not in (
        DocumentReview.State.PENDING,
        DocumentReview.State.APPROVED,
    ):
        return False
    review.state = DocumentReview.State.UNREVIEWED
    review.revision += 1
    review.reviewer = None
    review.submitted_by = None
    review.save(using=using)
    ReviewEvent.objects.using(using).create(
        review=review,
        action="invalidate",
        actor=actor,
        revision=review.revision,
        request_id=None,
        comment="",
    )
    return True


@receiver(pre_delete, sender=Document)
def delete_review_data(sender, instance, using, origin=None, **kwargs):
    # Soft deletion emits pre_delete without a collector origin.
    if origin is not None:
        ReviewRequest.objects.using(using).filter(document_id=instance.pk).delete()
        DocumentReview.objects.using(using).filter(document_id=instance.pk).delete()


def queue_queryset(actor):
    queryset = DocumentReview.objects.filter(
        revision__gt=0,
        document__deleted_at__isnull=True,
        document__root_document__isnull=True,
    )
    if not actor.is_active or not actor.has_perm("documents.view_document"):
        return queryset.none()
    return queryset.filter(document_id__in=permitted_document_ids(actor)).order_by(
        "document_id",
    )
