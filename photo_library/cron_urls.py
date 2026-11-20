from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from digest.urls import _heartbeat
from photo_library.cron import TagRecapPhotosView

urlpatterns = [
    path(
        "tag-recap-photos",
        csrf_exempt(_heartbeat("tag-recap-photos", TagRecapPhotosView.as_view())),
        name="photo-library-cron-tag",
    ),
]
