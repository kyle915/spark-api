from django.urls import path

from utils.thumb_views import thumb

urlpatterns = [path("thumb", thumb, name="img-thumb")]
