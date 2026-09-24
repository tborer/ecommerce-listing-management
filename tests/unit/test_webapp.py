"""API-level tests for the web app: auth, settings, encrypted credentials,
discovery -> CJ match -> criteria, manual + auto listing, cron. eBay and CJ
are faked at the module boundary; the DB is a throwaway SQLite file."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from ecommerce_listing_mgmt.ebay import browse
from ecommerce_listing_mgmt.suppliers import cj
from ecommerce_listing_mgmt.webapp import db as webdb
from ecommerce_listing_mgmt.webapp import ebay_account, engine
from ecommerce_listing_mgmt.webapp.main import app
from ecommerce_listing_mgmt.webapp.models import Credential, Run


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("ELM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CRON_SECRET", "cron-s3cret")
    monkeypatch.delenv("ALLOW_SIGNUP", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def signup(c, email="owner@example.com", pw="correct horse"):
    r = c.post("/api/auth/signup", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return r


# --- fakes -------------------------------------------------------------------

def api_item(i, title, price, cat="1281"):
    return browse.ApiItem(id=str(i), title=title, priceNum=price, currency="USD",
                          url=f"https://www.ebay.com/itm/{i}", source="deal", image_url=f"https://i/{i}.jpg",
                          category_id=cat, discount_pct=20.0)


class FakeCJClient:
    def __init__(self, catalog):
        self.catalog = catalog  # title -> (price, shipping, days)
        self.searches = []

    def search_products(self, keyword, page_size=20, warehouse_country=None):
        self.searches.append(keyword)
        return [cj.CJProduct(pid=f"P-{t}", title=t, price=v[0], image_url=f"https://cj/{t}.jpg")
                for t, v in self.catalog.items()]

    def get_product(self, pid):
        title = pid[2:]
        price = self.catalog[title][0]
        return cj.CJProductDetail(pid=pid, title=title, price=price, image_url=f"https://cj/{title}.jpg",
                                  description="Great product. CJdropshipping ships worldwide.",
                                  variants=[cj.CJVariant(vid=f"V-{title}", name="Default", price=price)])

    def freight_quote(self, vid, end_country="US", start_country="CN", quantity=1):
        title = vid[2:]
        _, ship, days = self.catalog[title]
        return [cj.FreightOption("CJPacket", ship, max(1, days - 3), days)]


@pytest.fixture
def fakes(monkeypatch):
    deals = [api_item(1, "Cat Tree Tower 3 Level Sisal Scratching Post", 60.0),
             api_item(2, "Stainless Steel Dog Bowl Set", 25.0),
             api_item(3, "Anti-snoring mouth tape 30 pcs", 12.0),
             api_item(4, "Ergonomic Office Chair Lumbar", 95.0)]
    monkeypatch.setattr(browse, "discover_deals_url", lambda url, n, ceiling, env, credential_mode: deals)
    monkeypatch.setattr(browse, "discover_keyword", lambda kw, n, ceiling, env, credential_mode: [])
    fake_cj = FakeCJClient({
        "Cat Tree Tower 3 Level Sisal Scratching Post Cat": (14.0, 6.0, 10),   # good margin
        "Stainless Steel Dog Bowl Set Pet": (19.0, 5.0, 12),                 # too thin
    })
    monkeypatch.setattr(engine, "make_cj_client", lambda db, user: fake_cj)
    published = []

    def fake_publish(db, user, cand, s):
        published.append(cand.id)
        return f"11{cand.id:08d}"

    monkeypatch.setattr(ebay_account, "publish_candidate", fake_publish)
    return {"cj": fake_cj, "published": published}


# --- tests -------------------------------------------------------------------

def test_first_user_can_sign_up_then_signup_closes(client):
    assert client.get("/api/auth/config").json() == {"signup_allowed": True}
    signup(client)
    assert client.get("/api/auth/me").json() == {"email": "owner@example.com"}
    other = TestClient(app)
    assert other.get("/api/auth/config").json() == {"signup_allowed": False}
    assert other.post("/api/auth/signup", json={"email": "x@example.com", "password": "12345678"}).status_code == 403


def test_login_logout_and_protection(client):
    signup(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-pass"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "OWNER@example.com", "password": "correct horse"}).status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_settings_defaults_and_validation(client):
    signup(client)
    body = client.get("/api/settings").json()
    s = body["settings"]
    assert s["auto_list_enabled"] is False and s["max_ebay_price"] == 100.0
    assert any(o["label"] == "Pet Supplies" for o in body["options"]["deal_categories"])
    s.update(min_ebay_price=50, max_ebay_price=40)
    assert client.put("/api/settings", json=s).status_code == 422
    s.update(min_ebay_price=5, max_ebay_price=80, keywords=[" neck fan ", "neck fan", ""])
    saved = client.put("/api/settings", json=s).json()["settings"]
    assert saved["keywords"] == ["neck fan"]
    s["deal_categories"] = ["https://evil.example/deals"]
    assert client.put("/api/settings", json=s).status_code == 422


def test_cj_key_stored_encrypted_and_never_returned(client, monkeypatch):
    signup(client)
    monkeypatch.setattr(cj.CJClient, "ensure_token", lambda self: "AT")
    r = client.put("/api/connections/cj", json={"api_key": "CJ999@api@supersecretvalue"})
    assert r.json()["ok"] is True and r.json()["key_hint"] == "…alue"
    assert "supersecret" not in client.get("/api/connections").text
    with webdb.new_session() as db:
        cred = db.scalar(select(Credential).where(Credential.provider == "cj"))
        assert "supersecret" not in cred.secret_enc


def test_cj_key_test_failure_is_reported(client, monkeypatch):
    signup(client)

    def bad(self):
        raise cj.CJError("Invalid API key", 1600001)

    monkeypatch.setattr(cj.CJClient, "ensure_token", bad)
    r = client.put("/api/connections/cj", json={"api_key": "CJ999@api@nope-nope"}).json()
    assert r["ok"] is False and "Invalid API key" in r["error"] and r["connected"] is True


def test_storing_credentials_requires_encryption_key(client, monkeypatch):
    signup(client)
    monkeypatch.delenv("ELM_ENCRYPTION_KEY")
    r = client.put("/api/connections/cj", json={"api_key": "CJ999@api@abcdefgh"})
    assert r.status_code == 503 and "ELM_ENCRYPTION_KEY" in r.json()["detail"]


def test_run_without_cj_key_errors_clearly(client, fakes, monkeypatch):
    signup(client)
    monkeypatch.setattr(engine, "make_cj_client", lambda db, user: (_ for _ in ()).throw(
        engine.RunBlocked("add your CJdropshipping API key under Connections")))
    run = client.post("/api/runs").json()
    assert run["status"] == "error" and "CJdropshipping API key" in run["error"]


def test_run_discovers_matches_and_evaluates(client, fakes):
    signup(client)
    run = client.post("/api/runs").json()
    assert run["status"] == "done", run
    assert run["stats"]["new_candidates"] == 3 and run["stats"]["excluded"] == 1

    review = client.get("/api/candidates?view=review").json()["items"]
    assert [c["ebay_item_id"] for c in review] == ["1"]
    cat_tree = review[0]
    assert cat_tree["cj_cost"] == 14.0 and cat_tree["shipping_cost"] == 6.0
    assert cat_tree["profit"] == round(60 - 60 * 0.17 - 14 - 6, 2)
    assert all(r["ok"] for r in cat_tree["criteria"])

    rejected = {c["ebay_item_id"]: c for c in client.get("/api/candidates?view=rejected").json()["items"]}
    assert rejected["2"]["status"] == "rejected"
    assert any(not r["ok"] and r["rule"] == "Target margin" for r in rejected["2"]["criteria"])
    assert rejected["4"]["status"] == "no_match"
    assert fakes["published"] == []  # auto-list is off by default

    # Re-running doesn't duplicate already-seen eBay items.
    again = client.post("/api/runs").json()
    assert again["stats"]["new_candidates"] == 0


def test_manual_list_and_dismiss(client, fakes):
    signup(client)
    client.post("/api/runs")
    cand = client.get("/api/candidates?view=review").json()["items"][0]
    listed = client.post(f"/api/candidates/{cand['id']}/list").json()
    assert listed["status"] == "listed" and listed["ebay_listing_url"].startswith("https://www.ebay.com/itm/11")
    assert client.post(f"/api/candidates/{cand['id']}/list").status_code == 409

    rejected = client.get("/api/candidates?view=rejected").json()["items"][0]
    assert client.post(f"/api/candidates/{rejected['id']}/dismiss").json()["status"] == "dismissed"
    assert client.post(f"/api/candidates/{rejected['id']}/restore").json()["status"] == rejected["status"]


def test_listing_failure_is_recorded(client, fakes, monkeypatch):
    signup(client)
    client.post("/api/runs")

    def fail(db, user, cand, s):
        raise ebay_account.ListingError("choose your eBay shipping, payment and return policies in Settings")

    monkeypatch.setattr(ebay_account, "publish_candidate", fail)
    cand = client.get("/api/candidates?view=review").json()["items"][0]
    out = client.post(f"/api/candidates/{cand['id']}/list").json()
    assert out["status"] == "list_failed" and "policies" in out["error"]
    assert client.get("/api/candidates?view=failed").json()["total"] == 1


def test_auto_list_top_n(client, fakes):
    signup(client)
    s = client.get("/api/settings").json()["settings"]
    s.update(auto_list_enabled=True, auto_list_max_per_run=5)
    client.put("/api/settings", json=s)
    run = client.post("/api/runs").json()
    assert run["status"] == "done" and run["stats"]["auto_list_attempted"] == 1
    listed = client.get("/api/candidates?view=listed").json()["items"]
    assert len(listed) == 1 and listed[0]["auto_listed"] is True


def test_run_resumes_across_calls_when_out_of_time(client, fakes, monkeypatch):
    signup(client)
    monkeypatch.setenv("ELM_RUN_BUDGET_SECONDS", "0.000001")
    run = client.post("/api/runs").json()
    assert run["status"] == "running" and run["phase"] == "match"
    monkeypatch.setenv("ELM_RUN_BUDGET_SECONDS", "30")
    done = client.post(f"/api/runs/{run['id']}/continue").json()
    assert done["status"] == "done"


def test_cron_requires_secret_and_runs_due_schedules(client, fakes):
    signup(client)
    s = client.get("/api/settings").json()["settings"]
    s.update(schedule_enabled=True, schedule_hour_utc=0)
    client.put("/api/settings", json=s)
    anon = TestClient(app)
    assert anon.get("/api/cron/tick").status_code == 401
    r = anon.get("/api/cron/tick", headers={"Authorization": "Bearer cron-s3cret"}).json()
    assert r == {"started": 1, "advanced": 1}
    r = anon.get("/api/cron/tick", headers={"Authorization": "Bearer cron-s3cret"}).json()
    assert r["started"] == 0  # already ran for today's slot
    with webdb.new_session() as db:
        assert db.scalar(select(Run).where(Run.trigger == "schedule")).status == "done"


def test_schedule_slot_math():
    now = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)
    assert engine.last_slot(now, 9) == datetime(2026, 9, 23, 9, tzinfo=timezone.utc)
    assert engine.last_slot(now, 14) == datetime(2026, 9, 22, 14, tzinfo=timezone.utc)


def test_users_cannot_see_each_others_candidates(client, fakes, monkeypatch):
    signup(client)
    client.post("/api/runs")
    cand_id = client.get("/api/candidates?view=review").json()["items"][0]["id"]
    monkeypatch.setenv("ALLOW_SIGNUP", "true")
    other = TestClient(app)
    signup(other, "second@example.com")
    assert other.get("/api/candidates?view=all").json()["total"] == 0
    assert other.post(f"/api/candidates/{cand_id}/list").status_code == 404


def test_listing_description_strips_supplier_mentions():
    from ecommerce_listing_mgmt.webapp.models import Candidate
    cand = Candidate(ebay_title="x", cj_title="Cat Tree CJ Edition",
                     details={"cj_description": "Great tree.\nShipped by CJdropshipping. Free returns!"})
    desc = ebay_account.listing_description(cand)
    assert "CJ" not in desc and "Free" not in desc and "<p>Great tree.</p>" in desc
    assert "CJ" not in ebay_account.listing_title(cand)


def test_api_home_redirects_to_website_when_configured(client, monkeypatch):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "SourceSnap API" in r.text  # no APP_BASE_URL: short page, not JSON 404
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    monkeypatch.setenv("APP_BASE_URL", "https://sourcesnap.vercel.app")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "https://sourcesnap.vercel.app/"
    # Never redirect to itself.
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert client.get("/", follow_redirects=False).status_code == 200


def test_api_home_works_on_vercel_without_a_database(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    c = TestClient(app, raise_server_exceptions=False)
    home = c.get("/", follow_redirects=False)
    assert home.status_code == 200 and "Root Directory" in home.text
    assert c.get("/favicon.ico").status_code == 204
    r = c.get("/api/auth/config")
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]
