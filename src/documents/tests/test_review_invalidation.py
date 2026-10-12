import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock

import pytest
from django.db import close_old_connections
from django.db import transaction
from rest_framework.test import APIClient

from documents.bulk_edit import merge_as_versions
from documents.models import Document
from documents.review_hooks import review_actor
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory


def mutation(client, document, action, revision, **kwargs):
    return client.post(
        f"/api/documents/{document.pk}/review/",
        {
            "action": action,
            "expected_revision": revision,
            "request_id": str(uuid.uuid4()),
            **kwargs,
        },
        format="json",
    )


def snapshot(client, document):
    response = client.get(f"/api/documents/{document.pk}/review/")
    assert response.status_code == 200, response.data
    return response.data


@pytest.fixture
def pending_review(db):
    submitter = UserFactory(superuser=True)
    reviewer = UserFactory(superuser=True)
    document = DocumentFactory()
    client = APIClient()
    client.force_authenticate(submitter)
    response = mutation(client, document, "submit", 0, reviewer=reviewer.pk)
    assert response.status_code == 200, response.data
    client.force_authenticate(reviewer)
    return client, document, submitter, reviewer


@pytest.fixture
def approved_review(pending_review):
    client, document, submitter, reviewer = pending_review
    response = mutation(client, document, "approve", 1)
    assert response.status_code == 200, response.data
    return client, document, submitter, reviewer


def assert_invalidated(client, document, revision, actor=None):
    data = snapshot(client, document)
    assert data["state"] == "unreviewed"
    assert data["revision"] == revision
    assert data["reviewer"] is None
    assert data["submitted_by"] is None
    assert data["history"][-1]["action"] == "invalidate"
    assert data["history"][-1]["actor"] == actor
    assert data["history"][-1]["comment"] == ""
    assert data["history"][-1]["request_id"] is None
    return data


def test_title_and_noop_content_preserve_approval(approved_review):
    client, document, _, _ = approved_review
    expected = snapshot(client, document)
    document.title = "A metadata-only edit"
    document.save(update_fields=["title"])
    document.save(update_fields=["content"])
    Document.objects.filter(pk=document.pk).update(content=document.content)
    assert snapshot(client, document) == expected


def test_content_save_invalidates_once_with_actor(approved_review):
    client, document, actor, _ = approved_review
    with review_actor(actor):
        document.content = "Changed synthetic text"
        document.save(update_fields=["content", "modified"])
        document.save(update_fields=["content", "modified"])
    assert_invalidated(client, document, 3, actor.pk)


def test_pending_content_change_invalidates(pending_review):
    client, document, _, _ = pending_review
    Document.objects.filter(pk=document.pk).update(content="Updated while pending")
    assert_invalidated(client, document, 2)


def test_reprocess_content_and_archive_update_invalidates_once(approved_review):
    client, document, _, _ = approved_review
    Document.objects.filter(pk=document.pk).update(
        content="Reprocessed content",
        archive_checksum="updated-archive-checksum",
    )
    assert_invalidated(client, document, 3)


def test_content_and_review_invalidation_rollback_together(approved_review):
    client, document, _, _ = approved_review
    expected = snapshot(client, document)
    content = document.content
    with pytest.raises(RuntimeError), transaction.atomic():
        Document.objects.filter(pk=document.pk).update(content="Must roll back")
        assert_invalidated(client, document, 3)
        raise RuntimeError("Synthetic processing failure")
    document.refresh_from_db()
    assert document.content == content
    assert snapshot(client, document) == expected


def test_new_version_invalidates_root_with_actor(approved_review):
    client, root, actor, _ = approved_review
    with review_actor(actor):
        version = DocumentFactory(root_document=root, version_index=1)
    assert_invalidated(client, root, 3, actor.pk)
    version.content = "Later version content"
    version.save(update_fields=["content"])
    assert snapshot(client, root)["revision"] == 3


def test_bulk_version_creation_invalidates_once(approved_review):
    client, root, _, _ = approved_review
    Document.objects.bulk_create(
        [
            Document(root_document=root, version_index=i, checksum=f"synthetic-{i}")
            for i in (1, 2)
        ],
    )
    assert_invalidated(client, root, 3)


def test_merge_as_versions_invalidates_target(approved_review):
    client, root, actor, _ = approved_review
    source = DocumentFactory()
    with mock.patch("documents.bulk_edit.bulk_update_documents.apply_async"):
        merge_as_versions([root.pk, source.pk], root_document_id=root.pk, user=actor)
    assert_invalidated(client, root, 3, actor.pk)


def test_version_content_bulk_update_invalidates_root(approved_review):
    client, root, _, _ = approved_review
    version = DocumentFactory(root_document=root, version_index=1)
    reviewer = UserFactory(superuser=True)
    assert mutation(client, root, "submit", 3, reviewer=reviewer.pk).status_code == 200
    client.force_authenticate(reviewer)
    assert mutation(client, root, "approve", 4).status_code == 200
    version.content = "A bulk-updated version"
    Document.objects.bulk_update([version], ["content"])
    assert_invalidated(client, root, 6)


def test_trash_restore_preserves_review_and_history(approved_review):
    client, document, _, _ = approved_review
    expected = snapshot(client, document)
    document.delete()
    response = mutation(client, document, "reopen", 2)
    assert response.status_code in (403, 404, 409)
    document = Document.deleted_objects.get(pk=document.pk)
    document.restore(strict=False)
    assert snapshot(client, document) == expected


@pytest.mark.django_db(transaction=True, reset_sequences=True, serialized_rollback=True)
def test_content_change_racing_approval_never_leaves_changed_content_approved():
    submitter = UserFactory(superuser=True)
    reviewer = UserFactory(superuser=True)
    document = DocumentFactory()
    client = APIClient()
    client.force_authenticate(submitter)
    assert (
        mutation(client, document, "submit", 0, reviewer=reviewer.pk).status_code == 200
    )
    barrier = Barrier(2)

    def approve():
        close_old_connections()
        try:
            local_client = APIClient()
            local_client.force_authenticate(reviewer)
            barrier.wait(timeout=10)
            return mutation(local_client, document, "approve", 1).status_code
        finally:
            close_old_connections()

    def change_content():
        close_old_connections()
        try:
            local_document = Document.objects.get(pk=document.pk)
            barrier.wait(timeout=10)
            local_document.content = "Concurrent synthetic content"
            local_document.save(update_fields=["content"])
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        decision = executor.submit(approve)
        change = executor.submit(change_content)
        assert decision.result(timeout=20) in (200, 409)
        change.result(timeout=20)
    data = snapshot(client, document)
    assert data["state"] == "unreviewed"
    assert data["history"][-1]["action"] == "invalidate"
    document.refresh_from_db()
    assert document.content == "Concurrent synthetic content"
