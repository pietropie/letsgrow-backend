"""GET /usage: consultas e créditos do mês para o mostrador do Início (UX item 16)."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import test_sem_api as sem
from app.api.v1 import admin_business, usage
from app.database import get_db
from app.models.credits import CreditTaskCost
from app.models.plan_config import PlanConfig
from app.services import credits as credit_service, plan_config_service
from app.services.usage import get_usage

run = sem.run


class UsageTests(sem.Base_):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(usage.router, prefix="/usage")
        self.app.include_router(admin_business.router, prefix="/admin")
        self.app.dependency_overrides[sem.get_current_user] = lambda: self.current
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.api = TestClient(self.app)
        self.addCleanup(self.api.close)
        self.owner.ai_queries_reset_at = self.now
        self.plan("free", queries=10, monthly=None)

    def plan(self, key, *, queries, monthly):
        run(plan_config_service.get_all_plan_configs(self.db))  # semeia os planos
        row = self.session.scalars(select(PlanConfig).where(PlanConfig.plan_key == key)).one()
        row.ai_queries_per_month = queries
        row.monthly_credits = monthly
        self.session.commit()
        plan_config_service._invalidate()

    def credits(self, *, monthly=10, cost=3, replace_limit=True):
        self.settings_row.credits_enabled = True
        self.settings_row.credits_replace_query_limit = replace_limit
        self.session.add(CreditTaskCost(task_key="chat_message", credits=cost, is_active=True))
        self.session.commit()
        self.plan("free", queries=10, monthly=monthly)

    def spend(self, now=None):
        quote, _ = run(credit_service.create_quote(self.db, self.owner, "chat_message", None, now=now))
        return run(credit_service.reserve(self.db, self.owner, quote.id, "chat_message", now=now))

    def usage(self):
        response = self.api.get("/usage")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ── modos ───────────────────────────────────────────────────────────────

    def test_queries_mode_counts_down_and_warns_from_80_percent(self):
        for used, near, exhausted in ((0, False, False), (7, False, False), (8, True, False), (10, True, True)):
            with self.subTest(used=used):
                self.owner.ai_queries_this_month = used
                self.session.commit()
                body = self.usage()
                self.assertEqual(body["mode"], "queries")
                self.assertEqual(body["queries"], {"used": used, "total": 10, "remaining": 10 - used})
                self.assertIsNone(body["credits"])
                self.assertEqual((body["near_limit"], body["exhausted"]), (near, exhausted))
        self.assertEqual(body["warning_threshold"], 0.8)

    def test_threshold_is_configurable_in_the_admin(self):
        self.owner.ai_queries_this_month = 8
        self.session.commit()
        self.assertTrue(self.usage()["near_limit"])
        patched = self.api.patch("/admin/app-settings", json={"usage_warning_threshold": 0.9}, headers=self.admin())
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["usage_warning_threshold"], 0.9)
        self.assertFalse(self.usage()["near_limit"])
        for bad in (0.2, 0.96):
            self.assertEqual(self.api.patch("/admin/app-settings", json={"usage_warning_threshold": bad},
                                            headers=self.admin()).status_code, 422)

    def test_unlimited_plan_and_credits_off(self):
        self.plan("free", queries=None, monthly=None)
        body = self.usage()
        self.assertEqual((body["mode"], body["queries"], body["credits"]), ("unlimited", None, None))
        self.assertEqual((body["near_limit"], body["exhausted"]), (False, False))

    def test_credits_disabled_shows_only_queries_even_with_monthly_credits(self):
        self.plan("free", queries=10, monthly=50)
        body = self.usage()
        self.assertEqual(body["mode"], "queries")
        self.assertIsNone(body["credits"])

    def test_credits_mode_reads_usage_from_the_ledger(self):
        self.credits(monthly=10, cost=3)
        body = self.usage()
        self.assertEqual(body["mode"], "credits")  # créditos substituem o limite de consultas
        self.assertIsNone(body["queries"])
        self.assertEqual(body["credits"], {"plan_total": 10, "plan_used": 0, "extra_balance": 0, "used_in_period": 0,
                                           "remaining": 10, "next_expiry": body["credits"]["next_expiry"]})
        self.assertIsNotNone(body["credits"]["next_expiry"])  # sem rollover, os do plano vencem na virada

        self.spend()
        self.spend()
        quote = self.spend()
        body = self.usage()["credits"]
        self.assertEqual((body["used_in_period"], body["plan_used"], body["remaining"]), (9, 9, 1))
        self.assertTrue(self.usage()["near_limit"])
        run(credit_service.release(self.db, quote, "falha técnica"))  # estorno volta para o saldo e sai do uso
        body = self.usage()
        self.assertEqual((body["credits"]["used_in_period"], body["credits"]["remaining"]), (6, 4))
        self.assertFalse(body["near_limit"])

    def test_purchased_credits_count_in_the_ratio_and_expiry(self):
        self.credits(monthly=10, cost=3)
        soon = self.now + timedelta(days=3)
        run(credit_service.grant_lot(self.db, self.owner.id, kind="purchase", amount=20, description="Pacote",
                                     expires_at=soon))
        self.session.commit()
        for _ in range(4):
            self.spend()  # 12 usados: 10 do plano primeiro, 2 dos comprados
        body = self.usage()
        credits = body["credits"]
        self.assertEqual((credits["plan_used"], credits["used_in_period"], credits["extra_balance"], credits["remaining"]),
                         (10, 12, 18, 18))
        self.assertFalse(body["near_limit"])  # 12 de 30
        expiry = credits["next_expiry"]
        self.assertEqual(expiry["credits"], 18)
        self.assertEqual(datetime.fromisoformat(expiry["at"].replace("Z", "+00:00")), soon)

    def test_exhausted_credits(self):
        self.credits(monthly=6, cost=3)
        self.spend()
        self.spend()
        body = self.usage()
        self.assertEqual(body["credits"]["remaining"], 0)
        self.assertEqual((body["near_limit"], body["exhausted"]), (True, True))

    def test_both_mode_when_credits_do_not_replace_the_query_limit(self):
        self.credits(monthly=10, cost=3, replace_limit=False)
        self.owner.ai_queries_this_month = 9
        self.session.commit()
        body = self.usage()
        self.assertEqual(body["mode"], "both")
        self.assertEqual(body["queries"]["remaining"], 1)
        self.assertEqual(body["credits"]["remaining"], 10)
        self.assertEqual((body["near_limit"], body["exhausted"]), (True, False))  # as consultas puxam o aviso

    def test_plan_without_monthly_credits_is_exhausted_until_a_purchase(self):
        self.credits(monthly=None)
        body = self.usage()
        self.assertEqual((body["mode"], body["credits"]["remaining"], body["exhausted"]), ("credits", 0, True))
        run(credit_service.grant_lot(self.db, self.owner.id, kind="purchase", amount=5, description="Pacote"))
        self.session.commit()
        body = self.usage()
        self.assertEqual((body["credits"]["plan_total"], body["credits"]["extra_balance"]), (0, 5))
        self.assertEqual((body["near_limit"], body["exhausted"]), (False, False))

    def test_credits_on_without_any_paid_task_do_not_limit(self):
        self.credits()
        self.session.query(CreditTaskCost).update({"is_active": False})
        self.session.commit()
        self.assertEqual(self.usage()["mode"], "queries")

    # ── virada do período ──────────────────────────────────────────────────

    def test_query_counter_from_last_month_reads_as_zero_until_the_worker_resets_it(self):
        self.owner.ai_queries_this_month = 10
        self.owner.ai_queries_reset_at = self.now.replace(day=1) - timedelta(days=3)
        self.session.commit()
        body = self.usage()
        self.assertEqual((body["queries"]["used"], body["queries"]["remaining"], body["exhausted"]), (0, 10, False))

    def test_credit_usage_restarts_with_the_new_period(self):
        self.credits(monthly=10, cost=3)
        self.spend()
        self.spend()
        this_month = run(get_usage(self.db, self.owner))
        self.assertEqual(this_month["credits"]["used_in_period"], 6)
        next_month = this_month["period"]["renews_at"] + timedelta(hours=1)
        later = run(get_usage(self.db, self.owner, now=next_month))
        self.assertEqual(later["credits"]["used_in_period"], 0)
        self.assertEqual(later["credits"]["plan_total"], 10)  # nova concessão do mês
        self.assertEqual(later["credits"]["remaining"], 10)  # os do mês passado venceram (sem rollover)
        self.assertEqual(later["period"]["start"], this_month["period"]["renews_at"])

    # ── autorização ────────────────────────────────────────────────────────

    def test_requires_login(self):
        app = FastAPI()
        app.include_router(usage.router, prefix="/usage")
        app.dependency_overrides[get_db] = lambda: self.db
        with TestClient(app) as client:
            self.assertIn(client.get("/usage").status_code, (401, 403))

    def test_each_user_sees_only_their_own_usage(self):
        self.owner.ai_queries_this_month = 9
        self.other.ai_queries_reset_at = self.now
        self.session.commit()
        self.current = self.other
        self.assertEqual(self.usage()["queries"]["used"], 0)


if __name__ == "__main__":
    unittest.main()
