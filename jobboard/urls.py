from django.urls import path

from jobboard import views

urlpatterns = [
    path(
        "jobs/release/<str:release_token>",
        views.public_shift_release,
        name="jobboard.release",
    ),
    path("jobs/<str:token>", views.public_job_board, name="jobboard.public"),
    path("jobs/<str:token>/apply", views.public_job_board_apply, name="jobboard.apply"),
]
