from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class CustomAuthenticationBackend(ModelBackend):
    """
    Authenticate with case-insensitive username/email matching.

    This backend keeps Django's default password check and active-user rules.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        if username is None:
            username = kwargs.get(user_model.USERNAME_FIELD)

        if username is None or password is None:
            return None
        username = str(username).strip()
        password = str(password).strip()
        if not username:
            return None

        query = Q(**{f"{user_model.USERNAME_FIELD}__iexact": username})
        if hasattr(user_model, "email"):
            query |= Q(email__iexact=username)

        try:
            user = user_model._default_manager.get(query)
        except user_model.DoesNotExist:
            # Run the hasher once to reduce timing differences with unknown users.
            user_model().set_password(password)
            return None
        except user_model.MultipleObjectsReturned:
            user = user_model._default_manager.filter(query).order_by("pk").first()
            if user is None:
                return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
