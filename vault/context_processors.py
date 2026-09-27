from .models import Document


def review_queue_count(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {
        "review_queue_count": Document.objects.filter(status=Document.Status.NEEDS_REVIEW).count(),
        "failed_count": Document.objects.filter(status=Document.Status.FAILED).count(),
    }
