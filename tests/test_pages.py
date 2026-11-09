from html.parser import HTMLParser

from fastapi.testclient import TestClient

from beets_ui import beets_ctx
from beets_ui.settings import Settings

from conftest import make_album

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}


class Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.errors.append(f"unclosed <{self.stack.pop()}> before </{tag}>")
            self.stack.pop()
        else:
            self.errors.append(f"stray </{tag}>")


def test_every_page_renders_with_balanced_html(env, fake_source):
    from beets_ui.main import create_app

    app = create_app(Settings.from_env(), background=False)
    c = TestClient(app)
    c.post("/login", data={"password": "secret"})
    for name in ("one", "two"):
        make_album(env["inbox"] / name)
    c.post("/inbox/scan")
    for name in ("one", "two"):
        c.post("/inbox/folder/preview", data={"f": name})
    app.state.jobs.run_pending_sync()
    c.post("/inbox/folder/import", data={"f": "one", "choice-0": "c:0"})
    app.state.jobs.run_pending_sync()
    c.post("/inbox/folder/import", data={"f": "two", "choice-0": "c:0"})
    app.state.jobs.run_pending_sync()
    album = beets_ctx.lib().albums()[0]
    item = next(iter(album.items()))
    job = app.state.state.jobs()[0]["id"]
    pages = [
        "/inbox",
        "/inbox?status=inlib",
        "/inbox/folder?f=one",
        "/inbox/folder?f=two",
        "/library",
        "/library?view=table",
        "/library/tracks",
        "/library/tracks?singletons=1",
        "/library/artists",
        "/library/artists/primary",
        "/library/artists/aliases",
        "/library/romanize",
        f"/library/album/{album.id}",
        f"/library/item/{item.id}",
        "/artwork",
        "/artwork/artists",
        f"/artwork/album/{album.id}",
        "/artwork/artist?name=Test%20Artist",
        "/console",
        "/jobs",
        f"/jobs/{job}",
    ]
    for url in pages:
        r = c.get(url)
        assert r.status_code == 200, url
        chk = Checker()
        chk.feed(r.text)
        assert not chk.errors, (url, chk.errors[:5])
        assert chk.stack == [], (url, chk.stack)
