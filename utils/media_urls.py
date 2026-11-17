from django.urls import path

from utils.media_views import media_object, play_video

urlpatterns = [
    path("play", play_video, name="media-play"),
    # Private-media reader: /api/public/media/o/<token>/<filename>
    path("o/<str:token>", media_object, name="media-object-bare"),
    path("o/<str:token>/<path:name>", media_object, name="media-object"),
]
