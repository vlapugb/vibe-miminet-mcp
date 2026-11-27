"""Local Miminet frontend for the Firefox integration test."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest


@pytest.fixture
def miminet_frontend(tmp_path, monkeypatch):
    upstream = os.environ.get("MIMINET_SOURCE")
    if not upstream:
        pytest.skip("Set MIMINET_SOURCE to a Miminet checkout to run the Firefox test.")
    source = Path(upstream).expanduser().resolve() / "front" / "src"
    assert (source / "app.py").is_file(), f"Miminet frontend missing at {source}"
    from sqlalchemy import Integer
    from werkzeug.security import generate_password_hash

    database = tmp_path / "test.sqlite"
    monkeypatch.syspath_prepend(str(source))
    monkeypatch.setenv("MODE", "dev")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URI", f"sqlite:///{database}")
    monkeypatch.setenv("EXTERNAL_BASE_URL", "/")
    monkeypatch.setenv("JWT_COOKIE_DOMAIN", "")
    monkeypatch.setenv("MOZ_HEADLESS", "1")
    monkeypatch.setenv(
        "PLAYWRIGHT_BROWSERS_PATH", os.getenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    )
    from app import app
    from database.models import Network, Simulate, User, db

    monkeypatch.setitem(app.config, "JWT_COOKIE_CSRF_PROTECT", True)
    monkeypatch.setattr(
        "miminet_network.PREVIEW_IMAGES_ROOT", str(tmp_path / "previews")
    )
    other_guid = str(uuid4())
    with app.app_context():
        assert db.engine.url.database == str(database)
        tables = [User.__table__, Network.__table__, Simulate.__table__]
        for table in tables:
            column = table.c.id
            monkeypatch.setattr(
                column, "type", column.type.with_variant(Integer, "sqlite")
            )
        db.metadata.create_all(db.engine, tables=tables)
        db.session.add_all(
            [
                User(
                    id=1,
                    nick="Student",
                    email="student@example.test",
                    password_hash=generate_password_hash("test"),
                ),
                User(id=2, nick="Other"),
            ]
        )
        db.session.flush()
        db.session.add(Network(author_id=2, guid=other_guid, share_mode=True))
        db.session.commit()

    origin = "https://miminet.test"
    http = app.test_client(use_cookies=False)

    async def serve(route):
        req = route.request
        url = urlsplit(req.url)
        if url.hostname != "miminet.test":
            return await route.abort()
        headers = await req.all_headers()
        response = http.open(
            url.path,
            query_string=url.query,
            method=req.method,
            headers=headers,
            data=req.post_data_buffer,
            base_url=origin,
        )
        outgoing = dict(response.headers)
        cookies = response.headers.getlist("Set-Cookie")
        if cookies:
            outgoing["Set-Cookie"] = "\n".join(cookies)
        if response.location:
            # Playwright bypasses route handlers for the next hop of an HTTP redirect.
            # Render Flask's actual destination at its final URL without opening a port.
            target = response.location
            response = http.get(target, headers=headers, base_url=origin)
            script = f"<head><script>history.replaceState(null, '', {json.dumps(target)})</script>"
            body = response.get_data().replace(b"<head>", script.encode(), 1)
            outgoing.pop("Location", None)
        else:
            response.direct_passthrough = False
            body = response.get_data()
        outgoing.pop("Content-Length", None)
        await route.fulfill(status=response.status_code, headers=outgoing, body=body)

    return origin, other_guid, serve
