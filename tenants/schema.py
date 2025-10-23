import strawberry
from gqlauth.user.queries import UserQueries, UserType
from django.contrib.auth import get_user_model
from .mutations import AmbassadorsCustomRegister, SparkCustomRegister, ClientsCustomRegister
from gqlauth.user import relay as mutations

#Spark Schema
@strawberry.django.type(model=get_user_model())
class QuerySpark:
    me: UserType = UserQueries.me
    public: UserType = UserQueries.public_user

@strawberry.type
class MutationSpark(SparkCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field

#Ambassadors Schema
@strawberry.django.type(model=get_user_model())
class QueryAmbassadors:
    me: UserType = UserQueries.me
    public: UserType = UserQueries.public_user

@strawberry.type
class MutationAmbassadors(AmbassadorsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field

#Clients Schemas
@strawberry.django.type(model=get_user_model())
class QueryClients:
    me: UserType = UserQueries.me
    public: UserType = UserQueries.public_user

@strawberry.type
class MutationClients(ClientsCustomRegister):
    verify_token = mutations.VerifyToken.field
    token_auth = mutations.ObtainJSONWebToken.field
    refresh_token = mutations.RefreshToken.field
    verify_account = mutations.VerifyAccount.field