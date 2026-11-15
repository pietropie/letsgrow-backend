"""Equipamentos e insumos (UX item 26c/26d, PLAYSTORE.md §2.3): Admin, indicações, "Já tenho" e toques."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import test_sem_api as sem
from app.api.v1 import admin_shop, shop as shop_api
from app.database import get_db
from app.models.base import Base
from app.models.environment_reading import EnvironmentReading
from app.models.shop import (
    SHOP_CATEGORIES, ShopCategory, ShopClickDaily, ShopDismissal, ShopProduct, ShopStore,
)

SHOP_TABLES = [ShopCategory.__table__, ShopStore.__table__, ShopProduct.__table__, ShopDismissal.__table__,
               ShopClickDaily.__table__]


class ShopBase(sem.Base_):
    def setUp(self):
        super().setUp()
        Base.metadata.create_all(self.engine, tables=SHOP_TABLES)
        self.session.add_all([ShopCategory(key=k, name=n, sort_order=i, is_active=True)
                              for i, (k, n) in enumerate(SHOP_CATEGORIES.items())])
        self.owner.age_confirmed_at = datetime.now(timezone.utc)
        self.session.commit()
        app = FastAPI()
        app.include_router(admin_shop.router, prefix="/admin")
        app.include_router(shop_api.router, prefix="/shop")
        app.dependency_overrides[sem.get_current_user] = lambda: self.current
        app.dependency_overrides[get_db] = lambda: self.db
        self.api = TestClient(app)
        self.addCleanup(self.api.close)

    def admin_call(self, method, path, **kwargs):
        return self.api.request(method, f"/admin/shop{path}", headers=self.admin(), **kwargs)

    def store(self, domain="lojajardim.com.br", kind="garden_agro"):
        response = self.admin_call("POST", "/stores", json={"name": "Loja Jardim", "domain": domain, "kind": kind})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def product(self, store, **extra):
        body = {"name": "Exaustor de duto 150 mm", "category_key": "ventilation", "spec_1": "Vazão de 400 m³/h",
                "spec_2": "Duto de 150 mm", "store_id": store["id"],
                "url": f"https://www.{store['domain']}/p/exaustor-150?tag=letsgrow-20",
                "label": "partner", "triggers": ["grow_missing_exhaust", "grow_humidity_high"],
                "reason": "Renova o ar do ambiente fechado.", **extra}
        return self.admin_call("POST", "/products", json=body)

    def enable(self, **switches):
        response = self.admin_call("PATCH", "/settings", json={"enabled": True, **switches})
        self.assertEqual(response.status_code, 200, response.text)

    def field_error(self, response):
        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()["detail"]
        return detail[0]["loc"][-1], detail[0]["msg"]


class AdminTests(ShopBase):
    def test_settings_start_closed_and_bobby_stays_off(self):
        body = self.admin_call("GET", "/settings").json()
        self.assertEqual(body, {"enabled": False, "page": True, "environment_block": True, "phase_block": True,
                                "account_entry": True, "bobby": False, "sponsored": False})
        self.assertEqual(self.field_error(self.admin_call("PATCH", "/settings", json={"bobby": True}))[0], "bobby")
        self.enable(page=False)
        self.assertEqual(self.admin_call("GET", "/settings").json()["page"], False)
        self.assertEqual(self.api.get("/admin/shop/settings").status_code, 403)  # sem token

    def test_ten_fixed_categories(self):
        categories = self.admin_call("GET", "/categories").json()
        self.assertEqual([c["key"] for c in categories], list(SHOP_CATEGORIES))
        self.assertEqual(self.field_error(self.admin_call("PATCH", "/categories/light", json={"name": "Luz 420"}))[0], "name")
        renamed = self.admin_call("PATCH", "/categories/light", json={"name": "Iluminação", "sort_order": 9})
        self.assertEqual((renamed.json()["name"], renamed.json()["sort_order"]), ("Iluminação", 9))
        self.assertEqual(self.admin_call("PATCH", "/categories/sementes", json={"name": "x"}).status_code, 404)

    def test_only_generalist_or_garden_stores(self):
        store = self.store()
        self.assertEqual(store["domain"], "lojajardim.com.br")
        self.assertEqual(self.field_error(self.admin_call("POST", "/stores", json={
            "name": "Grow Brasil", "domain": "lojabrasil.com.br", "kind": "garden_agro"}))[0], "name")
        self.assertEqual(self.field_error(self.admin_call("POST", "/stores", json={
            "name": "Loja", "domain": "headshop420.com", "kind": "generalist"}))[0], "domain")
        self.assertEqual(self.admin_call("POST", "/stores", json={
            "name": "Loja", "domain": "x.com", "kind": "growshop"}).status_code, 422)
        self.assertEqual(self.field_error(self.admin_call("POST", "/stores", json={
            "name": "Outra", "domain": "https://www.lojajardim.com.br/", "kind": "generalist"}))[0], "domain")  # repetido
        self.assertEqual(self.product(store).status_code, 201)
        self.assertEqual(self.admin_call("DELETE", f"/stores/{store['id']}").status_code, 409)

    def test_product_validation(self):
        store = self.store()
        created = self.product(store)
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual((body["store_name"], body["price_from"], body["price_checked_at"]), ("Loja Jardim", None, None))
        cases = [
            ({"url": "https://outraloja.com.br/p/1"}, "url"),
            ({"url": "https://lojajardim.com.br/"}, "url"),                      # página inicial
            ({"url": "http://lojajardim.com.br/p/1"}, "url"),
            ({"url": "https://lojajardim.com.br/p/1?email=a@b.c"}, "url"),        # dado pessoal na saída
            ({"name": "Kit cannabis indoor"}, "name"),
            ({"spec_1": "Ideal para a 420"}, "spec_1"),
            ({"spec_2": "Dobra a colheita"}, "spec_2"),
            ({"reason": "O melhor para maconha"}, "reason"),
            ({"name": "Balança de precisão"}, "name"),
            ({"name": "Curso de cultivo"}, "name"),
            ({"triggers": ["vende_mais"]}, "triggers"),
            ({"category_key": "sementes"}, "category_key"),
        ]
        for extra, field in cases:
            with self.subTest(extra=extra):
                self.assertEqual(self.field_error(self.product(store, **extra))[0], field)
        self.assertEqual(self.product(store, price_from=99).status_code, 422)  # preço reservado
        patched = self.admin_call("PATCH", f"/products/{body['id']}", json={"is_active": False, "reason": None})
        self.assertEqual((patched.json()["is_active"], patched.json()["reason"]), (False, None))
        self.assertEqual(self.field_error(self.admin_call("PATCH", f"/products/{body['id']}",
                                                          json={"url": "https://x.com/p"}))[0], "url")
        listed = self.admin_call("GET", "/products", params={"category": "ventilation", "active": "false"}).json()
        self.assertEqual([p["id"] for p in listed], [body["id"]])
        self.assertEqual(self.admin_call("DELETE", f"/products/{body['id']}").status_code, 204)

    def test_triggers_list(self):
        triggers = {t["key"]: t for t in self.admin_call("GET", "/triggers").json()}
        self.assertEqual(triggers["grow_missing_exhaust"]["scope"], "grow")
        self.assertEqual(triggers["plant_phase_flower"]["scope"], "plant")


class PublicTests(ShopBase):
    def setUp(self):
        super().setUp()
        self.store_row = self.store()
        self.exhaust = self.product(self.store_row).json()
        self.fertilizer = self.product(self.store_row, name="Fertilizante para floração", category_key="fertilizer",
                                       spec_1="NPK 5-10-10", spec_2=None, triggers=["plant_phase_flower"],
                                       url="https://lojajardim.com.br/p/fert", reason=None).json()
        self.sponsored = self.product(self.store_row, name="Ventilador de presilha", label="sponsored",
                                      triggers=["grow_missing_circulation"], url="https://lojajardim.com.br/p/vent").json()

    def test_age_gate_and_closed_area(self):
        self.owner.age_confirmed_at = None
        self.session.commit()
        response = self.api.get("/shop/products")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (403, "age_confirmation_required"))
        self.owner.age_confirmed_at = datetime.now(timezone.utc)
        self.session.commit()
        self.assertEqual(self.api.get("/shop/settings").json()["enabled"], False)
        for path in ("/shop/products", "/shop/categories", "/shop/recommendations"):
            self.assertEqual(self.api.get(path).json(), [], path)

    def test_products_page_respects_labels_and_switches(self):
        self.enable()
        settings = self.api.get("/shop/settings").json()
        self.assertTrue(settings["page"])
        self.assertIn("A compra, a entrega e a garantia são da loja", settings["notice"])
        products = self.api.get("/shop/products").json()
        self.assertEqual({p["name"] for p in products}, {"Exaustor de duto 150 mm", "Fertilizante para floração"})
        card = next(p for p in products if p["name"].startswith("Exaustor"))
        self.assertEqual((card["label_text"], card["store"]["domain"], card["specs"]),
                         ("Publicidade · podemos receber comissão", "lojajardim.com.br",
                          ["Vazão de 400 m³/h", "Duto de 150 mm"]))
        self.assertEqual(len(self.api.get("/shop/products", params={"category": "fertilizer"}).json()), 1)
        self.enable(sponsored=True)
        self.assertIn("Patrocinado", {p["label_text"] for p in self.api.get("/shop/products").json()})
        self.admin_call("PATCH", "/categories/fertilizer", json={"is_active": False})
        self.assertNotIn("Fertilizante para floração", {p["name"] for p in self.api.get("/shop/products").json()})
        self.enable(page=False)
        self.assertEqual(self.api.get("/shop/products").json(), [])

    def test_grow_recommendations_come_from_its_data(self):
        self.enable()
        self.grow.target_humidity_rh = 55
        now = datetime.now(timezone.utc)
        self.session.add_all([EnvironmentReading(id=uuid.uuid4(), grow_id=self.grow.id, user_id=self.owner.id,
                                                 observed_at=now - timedelta(hours=h), humidity_rh=68)
                              for h in (1, 5, 30)])
        self.session.commit()
        items = self.api.get("/shop/recommendations", params={"grow_id": str(self.grow.id)}).json()
        by_trigger = {i["trigger"]: i for i in items}
        self.assertEqual(by_trigger["grow_missing_exhaust"]["reason"], "Sala não tem exaustor cadastrado.")
        self.assertIn("A umidade em Sala ficou acima da faixa (55%) em 3 leituras", by_trigger["grow_humidity_high"]["reason"])
        self.assertEqual(by_trigger["grow_humidity_high"]["product"]["id"], self.exhaust["id"])
        self.assertNotIn("grow_missing_circulation", by_trigger)  # só produto patrocinado, desligado
        self.assertEqual(self.api.get("/shop/recommendations", params={"grow_id": str(self.other_grow.id)}).status_code, 404)
        self.enable(environment_block=False)
        self.assertEqual(self.api.get("/shop/recommendations", params={"grow_id": str(self.grow.id)}).json(), [])

    def test_plant_block_is_neutral(self):
        self.enable()
        items = self.api.get("/shop/recommendations", params={"plant_id": str(self.plant.id)}).json()
        self.assertEqual([(i["trigger"], i["reason"]) for i in items],
                         [("plant_phase_flower", "A sua planta está em floração.")])
        self.assertNotIn("Aurora", items[0]["reason"])  # o nome costuma ser o de uma genética (LOJA)
        page = self.api.get("/shop/recommendations").json()  # página: ambientes e plantas juntos
        self.assertEqual({i["trigger"] for i in page} >= {"grow_missing_exhaust", "plant_phase_flower"}, True)

    def test_already_have_saves_equipment_and_hides_the_suggestion(self):
        self.enable()
        self.grow.target_humidity_rh = 55
        now = datetime.now(timezone.utc)
        self.session.add_all([EnvironmentReading(id=uuid.uuid4(), grow_id=self.grow.id, user_id=self.owner.id,
                                                 observed_at=now - timedelta(hours=h), humidity_rh=70)
                              for h in (1, 2, 3)])
        self.session.commit()
        response = self.api.post("/shop/recommendations/have", json={
            "trigger": "grow_humidity_high", "grow_id": str(self.grow.id), "product_id": self.exhaust["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["equipment"], {"dehumidifier": True})
        self.session.expire_all()
        self.assertTrue(self.session.get(type(self.grow), self.grow.id).dehumidifier)
        triggers = {i["trigger"] for i in self.api.get("/shop/recommendations", params={"grow_id": str(self.grow.id)}).json()}
        self.assertNotIn("grow_humidity_high", triggers)
        self.assertIn("grow_missing_exhaust", triggers)
        # exaustor: o tipo depende do modelo, então só dispensa
        self.api.post("/shop/recommendations/have", json={"trigger": "grow_missing_exhaust", "grow_id": str(self.grow.id)})
        self.api.post("/shop/recommendations/have", json={"trigger": "grow_missing_exhaust", "grow_id": str(self.grow.id)})
        self.assertEqual(self.session.scalars(select(ShopDismissal).where(
            ShopDismissal.trigger == "grow_missing_exhaust")).all().__len__(), 1)
        self.assertEqual(self.api.post("/shop/recommendations/have", json={
            "trigger": "plant_phase_flower", "grow_id": str(self.grow.id)}).status_code, 422)
        self.assertEqual(self.api.post("/shop/recommendations/have", json={
            "trigger": "plant_phase_flower", "plant_id": str(self.other_plant.id)}).status_code, 404)

    def test_click_counts_per_product_and_day_without_user(self):
        self.enable()
        for _ in range(2):
            response = self.api.post(f"/shop/products/{self.exhaust['id']}/click")
            self.assertEqual(response.json()["url"], f"https://www.lojajardim.com.br/p/exaustor-150?tag=letsgrow-20")
        rows = self.session.scalars(select(ShopClickDaily)).all()
        self.assertEqual([(str(r.product_id), r.clicks) for r in rows], [(self.exhaust["id"], 2)])
        self.assertNotIn("user_id", ShopClickDaily.__table__.columns)
        clicks = self.admin_call("GET", "/clicks").json()
        self.assertEqual((clicks[0]["product_name"], clicks[0]["count"]), ("Exaustor de duto 150 mm", 2))
        self.assertEqual(self.api.post(f"/shop/products/{uuid.uuid4()}/click").status_code, 404)


if __name__ == "__main__":
    unittest.main()
