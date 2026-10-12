from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.models import DocumentReview
from documents.review import mutate_review
from documents.review import queue_queryset
from documents.review import review_response
from documents.review import validate_payload
from documents.review import visible_document


class DocumentReviewView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.document = visible_document(request.user, kwargs["pk"])

    def get(self, request, pk):
        return Response(review_response(self.document, request.user))

    def post(self, request, pk):
        validate_payload(request.data, self.document, request.user)
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
    if value < 1 or (maximum is not None and value > maximum):
        raise ValidationError(f"Invalid {name}.")
    return value


class ReviewQueueView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

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
        queryset = queue_queryset(request.user)
        if state is not None:
            queryset = queryset.filter(state=state)
        if reviewer is not None:
            queryset = queryset.filter(reviewer_id=reviewer)
        offset = (page - 1) * size
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
