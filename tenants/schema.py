import strawberry
from gqlauth.user.queries import UserQueries, UserType
from django.contrib.auth import get_user_model
from .mutations import CustomMutation
from gqlauth.user import relay as mutations

@strawberry.django.type(model=get_user_model())
class Query:
    me: UserType = UserQueries.me
    public: UserType = UserQueries.public_user

@strawberry.type
class Mutation(CustomMutation):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field