import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest
from django.contrib.auth.models import Group
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.db import connection
from django.db import transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import F
from guardian.shortcuts import remove_perm
from rest_framework.test import APIClient

from documents.models import Document
from documents.models import DocumentReview
from documents.models import ReviewEvent
from documents.models import ReviewRequest
from documents.review import invalidate_review
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global
from paperless_testing.permissions import grant_object

if TYPE_CHECKING:
    from django_softdelete.managers import GlobalQuerySet

pytestmark = pytest.mark.django_db


@pytest.fixture
def review_case(paperless_dirs):
    submitter = UserFactory()
    reviewer = UserFactory()
    for user in (submitter, reviewer):
        grant_global(user, "view_document", "change_document")
    document = DocumentFactory(owner=submitter)
    grant_object(reviewer, document, "view_document", "change_document")
    client = APIClient()
    client.force_authenticate(submitter)
    return document, submitter, reviewer, client


def endpoint(document):
    return f"/api/documents/{document.pk}/review/"


def payload(action="submit", revision=0, **kwargs) -> dict[str, Any]:
    return {
        "action": action,
        "expected_revision": revision,
        "request_id": str(uuid.uuid4()),
        **kwargs,
    }


def submit(case):
    document, _submitter, reviewer, client = case
    result = client.post(
        endpoint(document),
        payload(reviewer=reviewer.pk),
        format="json",
    )
    assert result.status_code == 200
    return result


def test_lazy_default_and_capabilities(review_case):
    document, _submitter, reviewer, client = review_case
    result = client.get(endpoint(document))
    assert result.status_code == 200
    assert result.data == {
        "document": document.pk,
        "state": "unreviewed",
        "revision": 0,
        "reviewer": None,
        "submitted_by": None,
        "history": [],
        "allowed_actions": ["submit"],
        "eligible_reviewers": [{"id": reviewer.pk, "username": reviewer.username}],
    }
    assert not DocumentReview.objects.exists()
    assert client.get("/api/review_queue/").data == {"count": 0, "results": []}


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_transitions_history_and_restart(review_case, decision):
    document, submitter, reviewer, client = review_case
    first = submit(review_case)
    assert first.data["revision"] == 1
    assert first.data["state"] == "pending"
    assert first.data["allowed_actions"] == []
    client.force_authenticate(reviewer)
    assert client.get(endpoint(document)).data["allowed_actions"] == [
        "approve",
        "reject",
    ]
    data = payload(
        decision,
        1,
        **(
            {"comment": "  Unicode feedback λ <script>alert(1)</script>  "}
            if decision == "reject"
            else {}
        ),
    )
    result = client.post(endpoint(document), data, format="json")
    assert result.status_code == 200
    assert result.data["state"] == ("approved" if decision == "approve" else "rejected")
    assert result.data["revision"] == 2
    assert result.data["history"][-1] == {
        "action": decision,
        "actor": reviewer.pk,
        "revision": 2,
        "at": result.data["history"][-1]["at"],
        "request_id": data["request_id"],
        "comment": data.get("comment", "").strip(),
    }
    assert result.data["history"][-1]["at"].endswith("Z")
    if decision == "reject":
        assert (
            client.post(
                endpoint(document),
                payload(reviewer=submitter.pk, revision=2),
                format="json",
            ).status_code
            == 409
        )
    client.force_authenticate(submitter)
    reopened = client.post(endpoint(document), payload("reopen", 2), format="json")
    assert reopened.status_code == 200
    assert reopened.data["reviewer"] is None
    assert reopened.data["submitted_by"] is None
    assert reopened.data["revision"] == 3
    assert [event["revision"] for event in reopened.data["history"]] == [1, 2, 3]
    fresh = APIClient()
    fresh.force_authenticate(User.objects.get(pk=submitter.pk))
    assert fresh.get(endpoint(document)).data == reopened.data
    event = ReviewEvent.objects.first()
    assert event is not None
    event.comment = "rewrite"
    with pytest.raises(ValidationError):
        event.save()


def test_replay_original_full_response_and_key_conflicts(review_case):
    document, submitter, reviewer, client = review_case
    data = payload(reviewer=reviewer.pk)
    first = client.post(endpoint(document), data, format="json")
    assert first.status_code == 200
    client.force_authenticate(reviewer)
    decision = payload("approve", 1)
    approved = client.post(endpoint(document), decision, format="json")
    assert approved.status_code == 200
    client.force_authenticate(submitter)
    assert client.post(endpoint(document), data, format="json").data == first.data
    assert (
        client.post(
            endpoint(document),
            {**data, "expected_revision": 2},
            format="json",
        ).status_code
        == 409
    )
    assert ReviewEvent.objects.count() == 2
    client.post(endpoint(document), payload("reopen", 2), format="json")
    client.force_authenticate(reviewer)
    assert (
        client.post(endpoint(document), decision, format="json").data == approved.data
    )
    assert ReviewEvent.objects.count() == 3


def test_replay_requires_current_global_and_object_access(review_case):
    document, _submitter, reviewer, client = review_case
    submit(review_case)
    client.force_authenticate(reviewer)
    data = payload("approve", 1)
    assert client.post(endpoint(document), data, format="json").status_code == 200
    remove_perm("change_document", reviewer, document)
    assert client.post(endpoint(document), data, format="json").status_code == 403
    remove_perm("view_document", reviewer, document)
    assert client.post(endpoint(document), data, format="json").status_code == 404
    grant_object(reviewer, document, "view_document", "change_document")
    reviewer.user_permissions.clear()
    client.force_authenticate(User.objects.get(pk=reviewer.pk))
    assert client.post(endpoint(document), data, format="json").status_code == 404
    assert ReviewEvent.objects.count() == 2


def test_replay_submission_survives_reviewer_deactivation(review_case):
    document, _, reviewer, client = review_case
    data = payload(reviewer=reviewer.pk)
    original = client.post(endpoint(document), data, format="json")
    reviewer.is_active = False
    reviewer.save()
    replay = client.post(endpoint(document), data, format="json")
    assert replay.status_code == 200
    assert replay.data == original.data
    assert ReviewEvent.objects.count() == 1


def test_assignment_validation_precedes_action_permissions(review_case):
    document, submitter, reviewer, client = review_case
    submitter.user_permissions.remove(
        *submitter.user_permissions.filter(codename="change_document"),
    )
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=submitter.pk),
            format="json",
        ).status_code
        == 400
    )
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=reviewer.pk),
            format="json",
        ).status_code
        == 403
    )


def test_stale_former_reviewer_after_invalidation_gets_conflict(review_case):
    document, submitter, reviewer, client = review_case
    submit(review_case)
    with transaction.atomic():
        Document.global_objects.filter(pk=document.pk).update(id=F("id"))
        invalidate_review(document)
    client.force_authenticate(reviewer)
    assert (
        client.post(
            endpoint(document),
            payload("approve", 1),
            format="json",
        ).status_code
        == 409
    )
    assert (
        client.post(
            endpoint(document),
            payload("approve", 2),
            format="json",
        ).status_code
        == 403
    )
    client.force_authenticate(submitter)
    assert (
        client.post(
            endpoint(document),
            payload("approve", 1),
            format="json",
        ).status_code
        == 403
    )
    assert DocumentReview.objects.get().revision == 2
    assert ReviewEvent.objects.count() == 2


def test_get_and_queue_use_current_permissions_not_user_cache(review_case):
    document, submitter, _, client = review_case
    submit(review_case)
    assert client.get(endpoint(document)).status_code == 200
    assert client.get("/api/review_queue/").data["count"] == 1
    submitter.user_permissions.clear()
    assert client.get(endpoint(document)).status_code == 404
    assert client.get("/api/review_queue/").data["count"] == 0


def test_version_review_uses_root(review_case):
    document, _submitter, reviewer, client = review_case
    version = DocumentFactory(root_document=document, version_index=1)
    data = payload(reviewer=reviewer.pk)
    result = client.post(endpoint(version), data, format="json")
    assert result.status_code == 200
    assert result.data["document"] == document.pk
    assert client.get(endpoint(document)).data == result.data
    assert not DocumentReview.objects.filter(document=version).exists()
    assert client.post(endpoint(document), data, format="json").data == result.data


def test_queue_huge_pagination_is_validation_error(review_case):
    client = review_case[3]
    assert client.get("/api/review_queue/?page=" + "9" * 30).status_code == 400
    assert (
        client.get(
            "/api/review_queue/?page=9223372036854775807&page_size=100",
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "change",
    [
        {"action": "other"},
        {"action": []},
        {"expected_revision": True},
        {"expected_revision": -1},
        {"expected_revision": "0"},
        {"expected_revision": 0.0},
        {"reviewer": True},
        {"reviewer": 0},
        {"reviewer": "1"},
        {"request_id": "not-uuid"},
        {"request_id": 1},
        {"request_id": None},
        {"state": "approved"},
        {"actor": 1},
        {"revision": 1},
        {"history": []},
        {"comment": "unexpected"},
    ],
)
def test_strict_submit_schema_atomic(review_case, change):
    document, _submitter, reviewer, client = review_case
    data = {**payload(reviewer=reviewer.pk), **change}
    assert client.post(endpoint(document), data, format="json").status_code == 400
    assert not DocumentReview.objects.exists()
    assert not ReviewEvent.objects.exists()
    assert not ReviewRequest.objects.exists()


@pytest.mark.parametrize(
    "missing",
    ["action", "expected_revision", "request_id", "reviewer"],
)
def test_required_fields(review_case, missing):
    document, _, reviewer, client = review_case
    data = payload(reviewer=reviewer.pk)
    del data[missing]
    assert client.post(endpoint(document), data, format="json").status_code == 400


@pytest.mark.parametrize("action", ["approve", "reject", "reopen"])
def test_action_specific_schema(review_case, action):
    document, _, reviewer, client = review_case
    data = payload(
        action,
        reviewer=reviewer.pk,
        **({"comment": "reason"} if action == "reject" else {}),
    )
    assert client.post(endpoint(document), data, format="json").status_code == 400
    if action != "reject":
        assert (
            client.post(
                endpoint(document),
                payload(action, comment="extra"),
                format="json",
            ).status_code
            == 400
        )


@pytest.mark.parametrize("comment", [None, True, 9, "", " \n\t ", "λ" * 2001])
def test_reject_comment_validation(review_case, comment):
    document, _, reviewer, client = review_case
    submit(review_case)
    client.force_authenticate(reviewer)
    assert (
        client.post(
            endpoint(document),
            payload("reject", 1, comment=comment),
            format="json",
        ).status_code
        == 400
    )
    assert DocumentReview.objects.get().revision == 1
    assert ReviewEvent.objects.count() == 1


def test_comment_unicode_trim_boundary(review_case):
    document, _, reviewer, client = review_case
    submit(review_case)
    client.force_authenticate(reviewer)
    result = client.post(
        endpoint(document),
        payload("reject", 1, comment=" " + "λ" * 2000 + " "),
        format="json",
    )
    assert result.status_code == 200
    assert result.data["history"][-1]["comment"] == "λ" * 2000


def test_assignment_validation_and_superuser_not_bypassing_assignment(review_case):
    document, submitter, reviewer, client = review_case
    candidates = [submitter, UserFactory(is_active=False), UserFactory(), UserFactory()]
    grant_global(candidates[-1], "view_document", "change_document")
    for candidate in candidates:
        assert (
            client.post(
                endpoint(document),
                payload(reviewer=candidate.pk),
                format="json",
            ).status_code
            == 400
        )
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=User.objects.latest("pk").pk + 100),
            format="json",
        ).status_code
        == 400
    )
    assert not ReviewEvent.objects.exists()
    submit(review_case)
    for actor in (submitter, UserFactory(superuser=True)):
        client.force_authenticate(actor)
        assert (
            client.post(
                endpoint(document),
                payload("approve", 1),
                format="json",
            ).status_code
            == 403
        )
    reviewer.is_active = False
    reviewer.save()
    client.force_authenticate(reviewer)
    assert client.post(
        endpoint(document),
        payload("approve", 1),
        format="json",
    ).status_code in (401, 403, 404)
    assert ReviewEvent.objects.count() == 1


def test_global_owner_unowned_and_group_access(review_case):
    document, submitter, reviewer, client = review_case
    submitter.user_permissions.clear()
    client.force_authenticate(User.objects.get(pk=submitter.pk))
    assert client.get(endpoint(document)).status_code == 404
    grant_global(submitter, "view_document")
    client.force_authenticate(User.objects.get(pk=submitter.pk))
    assert client.get(endpoint(document)).status_code == 200
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=reviewer.pk),
            format="json",
        ).status_code
        == 403
    )
    unowned = DocumentFactory()
    assert client.get(endpoint(unowned)).status_code == 200
    assert (
        client.post(
            endpoint(unowned),
            payload(reviewer=reviewer.pk),
            format="json",
        ).status_code
        == 403
    )
    outsider = UserFactory()
    grant_global(outsider, "view_document", "change_document")
    group = Group.objects.create(name="review group")
    outsider.groups.add(group)
    grant_object(group, document, "view_document", "change_document")
    client.force_authenticate(outsider)
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=reviewer.pk),
            format="json",
        ).status_code
        == 200
    )
    assert client.get(endpoint(document)).data["allowed_actions"] == []


def test_validation_order_and_unsupported_methods(review_case):
    document, submitter, reviewer, client = review_case
    private = DocumentFactory(owner=UserFactory())
    malformed = {"actor": "evil"}
    assert client.post(endpoint(private), malformed, format="json").status_code == 404
    assert (
        client.post(
            "/api/documents/999999999/review/",
            malformed,
            format="json",
        ).status_code
        == 404
    )
    submitter.user_permissions.remove(
        *submitter.user_permissions.filter(codename="change_document"),
    )
    client.force_authenticate(User.objects.get(pk=submitter.pk))
    assert client.post(endpoint(document), malformed, format="json").status_code == 400
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=reviewer.pk),
            format="json",
        ).status_code
        == 403
    )
    for method in (client.put, client.patch, client.delete):
        assert method(endpoint(document), {}, format="json").status_code == 405
        assert method(endpoint(private), {}, format="json").status_code == 404
    client.force_authenticate(None)
    assert client.get(endpoint(document)).status_code in (401, 403)


def test_conflicts_and_request_scopes(review_case):
    document, submitter, reviewer, client = review_case
    data = payload(reviewer=reviewer.pk)
    assert (
        client.post(
            endpoint(document),
            {**data, "expected_revision": 1},
            format="json",
        ).status_code
        == 409
    )
    assert not DocumentReview.objects.exists()
    assert client.post(endpoint(document), data, format="json").status_code == 200
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=reviewer.pk, revision=1),
            format="json",
        ).status_code
        == 409
    )
    another = DocumentFactory(owner=submitter)
    grant_object(reviewer, another, "view_document", "change_document")
    assert client.post(endpoint(another), data, format="json").status_code == 200
    client.force_authenticate(reviewer)
    same_key = payload("approve", 1, request_id=data["request_id"])
    assert client.post(endpoint(document), same_key, format="json").status_code == 200
    assert ReviewRequest.objects.count() == 3


def test_eligible_reviewers_include_group_and_superuser_but_not_inactive(review_case):
    document, _, reviewer, client = review_case
    group = Group.objects.create(name="eligible group")
    grouped = UserFactory()
    grouped.groups.add(group)
    grant_global(grouped, "view_document", "change_document")
    grant_object(group, document, "view_document", "change_document")
    admin = UserFactory(superuser=True)
    UserFactory(superuser=True, is_active=False)
    no_global = UserFactory()
    grant_object(no_global, document, "view_document", "change_document")
    candidates = client.get(endpoint(document)).data["eligible_reviewers"]
    assert {item["id"] for item in candidates} == {reviewer.pk, grouped.pk, admin.pk}
    assert (
        client.post(
            endpoint(document),
            payload(reviewer=grouped.pk),
            format="json",
        ).status_code
        == 200
    )
    client.force_authenticate(grouped)
    assert (
        client.post(
            endpoint(document),
            payload("approve", 1),
            format="json",
        ).status_code
        == 200
    )


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_existing_documents_versions_and_permissions(review_case):
    document, submitter, reviewer, client = review_case
    version = DocumentFactory(
        root_document=document,
        version_index=1,
        archive_checksum="synthetic-archive",
    )
    before = list(Document.global_objects.order_by("pk").values())
    executor = MigrationExecutor(connection)
    previous = [("documents", "0027_documentbarcode")]
    current = [("documents", "0028_document_review")]
    try:
        executor.migrate(previous)
        executor = MigrationExecutor(connection)
        executor.migrate(current)
        assert list(Document.global_objects.order_by("pk").values()) == before
        assert not DocumentReview.objects.exists()
        assert version.root_document_id == document.pk
        client.force_authenticate(reviewer)
        assert client.get(endpoint(document)).status_code == 200
        client.force_authenticate(submitter)
        assert (
            client.post(
                endpoint(document),
                payload(reviewer=reviewer.pk),
                format="json",
            ).status_code
            == 200
        )
    finally:
        MigrationExecutor(connection).migrate(current)


@pytest.mark.parametrize("target", ["pending", "approved"])
def test_invalidation_helper_atomic_and_nullable_actor(review_case, target):
    document, submitter, reviewer, client = review_case
    submit(review_case)
    if target == "approved":
        client.force_authenticate(reviewer)
        assert (
            client.post(
                endpoint(document),
                payload("approve", 1),
                format="json",
            ).status_code
            == 200
        )
    revision = DocumentReview.objects.get().revision
    with pytest.raises(RuntimeError), transaction.atomic():
        Document.global_objects.filter(pk=document.pk).update(id=F("id"))
        assert invalidate_review(document)
        raise RuntimeError("rollback")
    assert DocumentReview.objects.get().revision == revision
    with transaction.atomic():
        Document.global_objects.filter(pk=document.pk).update(id=F("id"))
        root = Document.global_objects.select_for_update().get(pk=document.pk)
        assert invalidate_review(root, submitter if target == "pending" else None)
        assert not invalidate_review(root)
    review = DocumentReview.objects.get()
    assert review.state == "unreviewed"
    assert review.reviewer_id is None
    assert review.submitted_by_id is None
    assert review.revision == revision + 1
    event = ReviewEvent.objects.last()
    assert event is not None
    assert event.action == "invalidate"
    assert event.request_id is None
    assert event.comment == ""
    assert event.actor_id == (submitter.pk if target == "pending" else None)


@pytest.mark.parametrize("queryset_delete", [True, False])
def test_trash_restore_and_permanent_delete(review_case, queryset_delete):
    document, _submitter, _reviewer, client = review_case
    before = submit(review_case).data
    document.delete()
    assert DocumentReview.objects.count() == 1
    assert ReviewEvent.objects.count() == 1
    assert ReviewRequest.objects.count() == 1
    result = client.get(endpoint(document))
    assert result.status_code == 200
    assert result.data["allowed_actions"] == []
    assert (
        client.post(endpoint(document), payload("reopen", 1), format="json").status_code
        == 403
    )
    assert client.get("/api/review_queue/").data["count"] == 0
    document = Document.global_objects.get(pk=document.pk)
    document.restore(strict=False)
    assert client.get(endpoint(document)).data == before
    if queryset_delete:
        cast(
            "GlobalQuerySet",
            Document.global_objects.filter(pk=document.pk),
        ).hard_delete()
    else:
        document.hard_delete()
    assert not DocumentReview.objects.exists()
    assert not ReviewEvent.objects.exists()
    assert not ReviewRequest.objects.exists()


def test_queue_permissions_filters_and_pagination(review_case):
    document, submitter, reviewer, client = review_case
    submit(review_case)
    other = DocumentFactory(owner=UserFactory())
    DocumentReview.objects.create(
        document=other,
        revision=1,
        state="pending",
        reviewer=reviewer,
    )
    final = DocumentFactory(owner=submitter)
    DocumentReview.objects.create(
        document=final,
        revision=2,
        state="rejected",
        reviewer=reviewer,
    )
    DocumentFactory(owner=submitter)
    zero = DocumentFactory(owner=submitter)
    DocumentReview.objects.create(document=zero)
    result = client.get("/api/review_queue/?page_size=1")
    assert result.data["count"] == 2
    assert result.data["results"] == [
        {
            "document": document.pk,
            "state": "pending",
            "reviewer": reviewer.pk,
            "revision": 1,
        },
    ]
    assert (
        client.get("/api/review_queue/?page_size=1&page=2").data["results"][0][
            "document"
        ]
        == final.pk
    )
    assert (
        client.get(f"/api/review_queue/?state=pending&reviewer={reviewer.pk}").data[
            "count"
        ]
        == 1
    )
    assert client.get("/api/review_queue/?page=999").data == {"count": 2, "results": []}
    submitter.user_permissions.clear()
    client.force_authenticate(User.objects.get(pk=submitter.pk))
    assert client.get("/api/review_queue/").data == {"count": 0, "results": []}


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-1",
        "page=x",
        "page=1.5",
        "page=",
        "page_size=0",
        "page_size=101",
        "page_size=true",
        "reviewer=0",
        "reviewer=-1",
        "reviewer=x",
        "reviewer=true",
        "state=bogus",
        "state=",
        "other=x",
        "state=pending&state=approved",
    ],
)
def test_queue_rejects_invalid_queries(review_case, query):
    assert review_case[3].get(f"/api/review_queue/?{query}").status_code == 400


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("identical", [True, False])
def test_concurrent_decisions_and_identical_retries(review_case, identical):
    document, _submitter, reviewer, _client = review_case
    submit(review_case)
    first = payload("approve", 1)
    second = first if identical else payload("reject", 1, comment="different decision")
    barrier = Barrier(2)

    def decide(data):
        close_old_connections()
        try:
            actor = User.objects.get(pk=reviewer.pk)
            concurrent_client = APIClient()
            concurrent_client.force_authenticate(actor)
            barrier.wait(timeout=10)
            result = concurrent_client.post(endpoint(document), data, format="json")
            return result.status_code, result.data
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(decide, data) for data in (first, second)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(status for status, _ in results) == (
        [200, 200] if identical else [200, 409]
    )
    if identical:
        assert results[0][1] == results[1][1]
    assert DocumentReview.objects.get().revision == 2
    assert ReviewEvent.objects.count() == 2
