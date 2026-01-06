from .models import Notification


def unread_notifications(request):
    """Context processor that provides unread notifications for the logged-in user."""
    if not request.user or not request.user.is_authenticated:
        return {}

    unread_qs = Notification.objects.filter(recipient=request.user, is_read=False)
    
    context = {
        'unread_notifications_count': unread_qs.count(),
        'unread_notifications': unread_qs[:6],
    }

    # Add low stock info for staff
    if request.user.role in ['admin', 'gso_staff']:
        from .models import Supply
        from django.db.models import F
        low_stock_qs = Supply.objects.filter(quantity__lte=F('min_stock_level'))
        context['low_stock_count'] = low_stock_qs.count()
        context['low_stock_items_global'] = low_stock_qs[:5]

    return context
