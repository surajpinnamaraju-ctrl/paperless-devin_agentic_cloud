import sys

from django.contrib.auth.models import User
from drf_spectacular.utils import OpenApiParameter
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.models import DocumentReview
from documents.review import mutate_review
from documents.review import queue_queryset
from documents.review import read_review
from documents.review import retry_sqlite
from documents.review import validate_payload
from documents.review import validate_reviewer
from documents.review import visible_document


class ReviewEventSerializer(serializers.Serializer):
    action = serializers.CharField()
    actor = serializers.IntegerField(allow_null=True)
    revision = serializers.IntegerField(min_value=1)
    at = serializers.DateTimeField()
    request_id = serializers.UUIDField(allow_null=True)
    comment = serializers.CharField(allow_blank=True)


class EligibleReviewerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()


class ReviewSerializer(serializers.Serializer):
    document = serializers.IntegerField()
    state = serializers.ChoiceField(choices=DocumentReview.State.choices)
    revision = serializers.IntegerField(min_value=0)
    reviewer = serializers.IntegerField(allow_null=True)
    submitted_by = serializers.IntegerField(allow_null=True)
    history = ReviewEventSerializer(many=True)
    allowed_actions = serializers.ListField(child=serializers.CharField())
    eligible_reviewers = EligibleReviewerSerializer(many=True)


class ReviewMutationSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("submit", "approve", "reject", "reopen"))
    expected_revision = serializers.IntegerField(min_value=0)
    request_id = serializers.UUIDField()
    reviewer = serializers.IntegerField(min_value=1, required=False)
    comment = serializers.CharField(max_length=2000, required=False)


class ReviewQueueItemSerializer(serializers.Serializer):
    document = serializers.IntegerField()
    state = serializers.ChoiceField(choices=DocumentReview.State.choices)
    reviewer = serializers.IntegerField(allow_null=True)
    revision = serializers.IntegerField(min_value=1)


class ReviewQueueSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    results = ReviewQueueItemSerializer(many=True)


class DocumentReviewView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        request.user = retry_sqlite(
            lambda: User.objects.get(pk=request.user.pk),
            "default",
        )
        self.document = visible_document(request.user, kwargs["pk"])

    @extend_schema(responses=ReviewSerializer)
    def get(self, request, pk):
        return Response(read_review(self.document, request.user))

    @extend_schema(request=ReviewMutationSerializer, responses=ReviewSerializer)
    def post(self, request, pk):
        validate_payload(request.data, self.document, request.user)
        validate_reviewer(request.data, self.document, request.user)
        return Response(mutate_review(self.document, request.user, request.data))


def query_integer(params, name, default=None, maximum=None):
    value = params.get(name, default)
    if value is None:
        return None
    if (
        not isinstance(value, (str, int))
        or not str(value).isascii()
        or not str(value).isdigit()
    ):
        raise ValidationError(f"Invalid {name}.")
    try:
        value = int(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid {name}.") from exc
    if value < 1 or value > (maximum if maximum is not None else sys.maxsize):
        raise ValidationError(f"Invalid {name}.")
    return value


class ReviewQueueView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    @extend_schema(
        parameters=[
            OpenApiParameter("state", str, enum=list(DocumentReview.State.values)),
            OpenApiParameter("reviewer", int),
            OpenApiParameter("page", int),
            OpenApiParameter("page_size", int),
        ],
        responses=ReviewQueueSerializer,
    )
    def get(self, request):
        params = request.query_params
        if set(params) - {"state", "reviewer", "page", "page_size"} or any(
            len(params.getlist(key)) != 1 for key in params
        ):
            raise ValidationError("Unknown or repeated filters.")
        state = params.get("state")
        if state is not None and state not in DocumentReview.State.values:
            raise ValidationError("Invalid state.")
        reviewer = query_integer(params, "reviewer")
        page = query_integer(params, "page", 1)
        size = query_integer(params, "page_size", 20, maximum=100)
        assert page is not None and size is not None
        actor = User.objects.get(pk=request.user.pk)
        queryset = queue_queryset(actor)
        if state is not None:
            queryset = queryset.filter(state=state)
        if reviewer is not None:
            queryset = queryset.filter(reviewer_id=reviewer)
        offset = (page - 1) * size
        if offset > sys.maxsize - size:
            raise ValidationError("Invalid page.")
        return Response(
            {
                "count": queryset.count(),
                "results": [
                    {
                        "document": review.document_id,
                        "state": review.state,
                        "reviewer": review.reviewer_id,
                        "revision": review.revision,
                    }
                    for review in queryset[offset : offset + size]
                ],
            },
        )
