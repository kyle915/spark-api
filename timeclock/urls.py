from django.urls import path

from timeclock import views

urlpatterns = [
    path("clock/<str:token>", views.public_clock_context, name="timeclock.public"),
    path("clock/<str:token>/in", views.public_clock_in, name="timeclock.clock_in"),
    path("clock/<str:token>/out", views.public_clock_out, name="timeclock.clock_out"),
]
