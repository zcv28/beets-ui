from __future__ import annotations

import httpx
import respx

from beets_ui.slskd import Slskd, flatten_downloads, group_responses

RESPONSES = [
    {
        "username": "alice",
        "hasFreeUploadSlot": True,
        "queueLength": 0,
        "uploadSpeed": 500000,
        "files": [
            {
                "filename": "Music\\Artist\\Album (2020)\\01 a.flac",
                "size": 1000,
                "extension": "flac",
            },
            {
                "filename": "Music\\Artist\\Album (2020)\\02 b.flac",
                "size": 2000,
                "extension": "flac",
            },
            {"filename": "Music\\Artist\\Album (2020)\\cover.jpg", "size": 50, "extension": "jpg"},
        ],
        "lockedFiles": [],
    },
    {
        "username": "bob",
        "hasFreeUploadSlot": False,
        "queueLength": 5,
        "uploadSpeed": 100000,
        "files": [
            {"filename": "share\\Album\\01.mp3", "size": 300, "bitRate": 320},
            {"filename": "share\\Album\\02.mp3", "size": 300, "bitRate": 320},
        ],
    },
]


def test_group_responses():
    groups = group_responses(RESPONSES)
    assert [g["username"] for g in groups] == ["alice", "bob"]
    alice = groups[0]
    assert alice["folder_name"] == "Album (2020)"
    assert len(alice["files"]) == 2  # cover.jpg filtered out
    assert alice["format"] == "FLAC" and alice["lossless"]
    bob = groups[1]
    assert bob["bitrate"] == 320 and bob["format"] == "MP3"
    everything = group_responses(RESPONSES, audio_only=False)
    assert len(everything[0]["files"]) == 3


@respx.mock
def test_client_search_and_enqueue():
    base = "http://slskd:5030/api/v0"
    search = respx.post(f"{base}/searches").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__regex=rf"{base}/searches/.*/responses").mock(
        return_value=httpx.Response(200, json=RESPONSES)
    )
    enqueue = respx.post(f"{base}/transfers/downloads/alice").mock(return_value=httpx.Response(201))
    c = Slskd("http://slskd:5030", "key")
    sid = c.search("artist album")
    assert search.calls[0].request.headers["X-API-Key"] == "key"
    groups = group_responses(c.search_responses(sid), audio_only=False)
    c.enqueue("alice", groups[0]["files"])
    import json

    body = json.loads(enqueue.calls[0].request.content)
    assert {f["filename"] for f in body} >= {"Music\\Artist\\Album (2020)\\01 a.flac"}


def test_flatten_downloads():
    data = [
        {
            "username": "alice",
            "directories": [
                {
                    "directory": "Music\\Artist\\Album",
                    "files": [
                        {
                            "id": "1",
                            "filename": "Music\\Artist\\Album\\01.flac",
                            "size": 100,
                            "bytesTransferred": 100,
                            "state": "Completed, Succeeded",
                        },
                        {
                            "id": "2",
                            "filename": "Music\\Artist\\Album\\02.flac",
                            "size": 100,
                            "bytesTransferred": 50,
                            "state": "InProgress",
                            "percentComplete": 50,
                        },
                    ],
                }
            ],
        }
    ]
    rows = flatten_downloads(data)
    assert rows[0]["state"] == "downloading"
    assert rows[0]["percent"] == 75.0
    data[0]["directories"][0]["files"][1]["state"] = "Completed, Succeeded"
    assert flatten_downloads(data)[0]["state"] == "done"
