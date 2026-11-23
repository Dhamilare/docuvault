from django.db.models import Count, Q
from .models import Document


def review_queue_count(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    stats = Document.objects.aggregate(
        review_queue_count=Count("id", filter=Q(status=Document.Status.NEEDS_REVIEW)),
        failed_count=Count("id", filter=Q(status=Document.Status.FAILED)),
    )
    return {
        "review_queue_count": stats["review_queue_count"],
        "failed_count": stats["failed_count"],
    }