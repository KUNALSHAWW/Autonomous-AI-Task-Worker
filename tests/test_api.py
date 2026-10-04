from fastapi.testclient import TestClient


def test_api_basics(sandbox, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from worker import api
    c = TestClient(api.app)
    h = c.get("/api/health").json()
    assert h["sandbox"] is True
    assert c.get("/acme/mail/").status_code == 200
    assert len(c.get("/api/examples").json()) >= 5
    r = c.post("/api/runs", json={"task": "do something"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert c.post(f"/api/runs/{rid}/reply", json={"answer": "x"}).status_code in (404, 409)
    assert c.get("/api/runs/nope").status_code == 404
    assert c.get(f"/api/runs/{rid}/files/../../etc/passwd").status_code == 404
