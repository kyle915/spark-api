"""Inline recap-video playback redirects."""

from django.test import RequestFactory
from django.urls import resolve

from utils import media_views
from utils.media_views import play_video, playback_content_type


class TestPlaybackContentType:
    def test_mov_is_served_as_mp4(self):
        assert playback_content_type("checkin/clip.mov") == "video/mp4"
        assert playback_content_type("checkin/clip.MOV") == "video/mp4"
        assert playback_content_type("checkin/clip.m4v") == "video/mp4"
        assert playback_content_type("checkin/clip.mp4") == "video/mp4"

    def test_webm_stays_webm(self):
        assert playback_content_type("checkin/clip.webm") == "video/webm"

    def test_photos_are_not_video(self):
        assert playback_content_type("checkin/shot.jpg") is None
        assert playback_content_type("checkin/shot.heic") is None


class TestPlayEndpoint:
    def test_route_is_registered(self):
        match = resolve("/api/public/media/play")
        assert match.func == play_video

    def test_mov_redirects_inline_as_mp4(self, monkeypatch):
        captured: dict[str, str | None] = {}

        def fake(blob, expiration_minutes=60, *, response_type=None, response_disposition=None):
            captured["blob"] = blob
            captured["response_type"] = response_type
            captured["response_disposition"] = response_disposition
            return "https://signed.example/clip"

        monkeypatch.setattr(media_views, "generate_download_url", fake)
        request = RequestFactory().get(
            "/api/public/media/play",
            {
                "path": "https://storage.googleapis.com/sparkio-production/checkin/Activation.mov",
            },
        )
        resp = play_video(request)
        assert resp.status_code == 302
        assert resp["Location"] == "https://signed.example/clip"
        assert captured["blob"] == "checkin/Activation.mov"
        assert captured["response_type"] == "video/mp4"
        assert captured["response_disposition"] == "inline"
        assert "attachment" not in (captured["response_disposition"] or "")

    def test_rejects_photos_and_traversal(self):
        photo = play_video(
            RequestFactory().get("/api/public/media/play", {"path": "checkin/shot.jpg"})
        )
        assert photo.status_code == 404
        escaped = play_video(
            RequestFactory().get(
                "/api/public/media/play", {"path": "../secrets/clip.mp4"}
            )
        )
        assert escaped.status_code == 400

    def test_falls_back_to_public_url_when_signing_fails(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("no signer")

        monkeypatch.setattr(media_views, "generate_download_url", boom)
        monkeypatch.setattr(
            media_views,
            "public_url",
            lambda name: f"https://storage.googleapis.com/bucket/{name}",
        )
        resp = play_video(
            RequestFactory().get("/api/public/media/play", {"path": "checkin/clip.mp4"})
        )
        assert resp.status_code == 302
        assert resp["Location"].endswith("checkin/clip.mp4")
