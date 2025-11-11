import strawberry
from django.contrib.auth import get_user_model
from gqlauth.user import relay as mutations
from gqlauth.user.queries import UserQueries

from .models import Role
from .mutations import (
    AmbassadorsCustomRegister,
    ClientsCustomRegister,
    SparkCustomRegister,
)

User = get_user_model()


@strawberry.django.type(Role)
class RoleType:
    id: strawberry.auto
    uuid: strawberry.auto
    name: strawberry.auto


@strawberry.django.type(model=get_user_model(), name="CustomUserType")
class CustomUserType:
    id: strawberry.auto
    uuid: strawberry.auto
    username: strawberry.auto
    email: strawberry.auto
    first_name: strawberry.auto
    last_name: strawberry.auto
    role: RoleType


# Spark Schema
@strawberry.type
class QuerySpark:
    @strawberry.field
    def me(self, info) -> CustomUserType:
        return info.context.request.user


@strawberry.type
class MutationSpark(SparkCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Ambassadors Schema
@strawberry.django.type(model=get_user_model())
class QueryAmbassadors:
    me: CustomUserType = UserQueries.me
    public: CustomUserType = UserQueries.public_user


@strawberry.type
class MutationAmbassadors(AmbassadorsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field


# Clients Schemas
@strawberry.django.type(model=get_user_model())
class QueryClients:
    me: CustomUserType = UserQueries.me
    public: CustomUserType = UserQueries.public_user


@strawberry.type
class MutationClients(ClientsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field
