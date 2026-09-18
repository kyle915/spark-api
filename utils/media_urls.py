from django.urls import path

from utils.media_views import play_video

urlpatterns = [path("play", play_video, name="media-play")]
