"""Role checks. Admin sees everything; trainers see their own schedule and
clients; clients see only their own data."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import ClientProfile, User


def role_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            allowed = user.role in roles or (User.Role.ADMIN in roles and user.is_superuser)
            if not allowed:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


client_required = role_required(User.Role.CLIENT)
staff_required = role_required(User.Role.ADMIN, User.Role.TRAINER)
admin_required = role_required(User.Role.ADMIN)


def visible_clients(user):
    """Clients a staff user may see."""
    qs = ClientProfile.objects.select_related("user", "primary_trainer__user")
    if user.is_studio_admin:
        return qs
    if user.is_trainer:
        t = user.trainer
        return qs.filter(Q(primary_trainer=t) | Q(bookings__event__trainer=t)).distinct()
    return qs.none()


def can_manage_event(user, event):
    return user.is_studio_admin or (user.is_trainer and event.trainer_id == user.trainer.pk)


def can_manage_booking(user, booking):
    if user.is_client:
        return booking.client.user_id == user.pk
    return can_manage_event(user, booking.event)
