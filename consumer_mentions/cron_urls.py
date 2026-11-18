from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from consumer_mentions.cron import TagConsumerMentionsView
from digest.urls import _heartbeat

urlpatterns = [
    path(
        "tag-consumer-mentions",
        csrf_exempt(_heartbeat("tag-consumer-mentions", TagConsumerMentionsView.as_view())),
        name="consumer-mentions-cron-tag",
    ),
]
