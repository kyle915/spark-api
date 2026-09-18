"""Walk-up upload-url content types.

Android Chrome reports a lot of gallery photos as ``application/octet-stream``.
The signed PUT must use the type the browser will actually send, so the
allowlist accepts that string instead of rewriting it.
"""

from events.checkin_views import checkin_upload_content_type


def test_jpeg_and_heic_still_sign():
    assert checkin_upload_content_type("image/jpeg") == "image/jpeg"
    assert checkin_upload_content_type(" image/heic ") == "image/heic"
    assert checkin_upload_content_type("image/heic-sequence") == "image/heic-sequence"


def test_android_octet_stream_is_accepted_as_sent():
    assert (
        checkin_upload_content_type("application/octet-stream")
        == "application/octet-stream"
    )


def test_non_media_is_rejected():
    assert checkin_upload_content_type("text/html") is None
    assert checkin_upload_content_type("application/javascript") is None
    assert checkin_upload_content_type("") is None
