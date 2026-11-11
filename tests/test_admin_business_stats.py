"""Agregações de negócio e limites de data da rota administrativa."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1 import admin_stats
from app.config import settings
from app.database import get_db
from app.models.base import Base
from app.models.billing import Subscription
from app.models.credits import CreditPackage, CreditPurchase
from app.models.user import User
from test_private_access import AsyncSessionAdapter


NOW = datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc)
HEADERS = {"X-Admin-Token": "token-de-teste"}
URL = "/admin/stats/business"


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class BusinessStatsTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def register_timezone(connection, _):
            def local_timestamp(zone, value):
                if value is not None:
                    return utc(value).astimezone(ZoneInfo(zone)).replace(tzinfo=None).isoformat()

            connection.create_function("timezone", 2, local_timestamp, deterministic=True)

        Base.metadata.create_all(engine, tables=[
            model.__table__ for model in (User, Subscription, CreditPackage, CreditPurchase)
        ])
        self.session = Session(engine)
        app = FastAPI()
        app.include_router(admin_stats.router, prefix="/admin")
        app.dependency_overrides[get_db] = lambda: AsyncSessionAdapter(self.session)
        self.client = TestClient(app)
        self.addCleanup(engine.dispose)
        self.addCleanup(self.session.close)
        self.addCleanup(self.client.close)
        self.enterContext(patch.object(settings, "ADMIN_TOKEN", HEADERS["X-Admin-Token"]))
        self.enterContext(patch.object(settings, "BILLING_GRACE_DAYS", 3))

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz)

        self.enterContext(patch.object(admin_stats, "datetime", FixedDateTime))

    def user(self, created_at):
        user_id = uuid.uuid4()
        self.session.add(User(id=user_id, username=str(user_id), email=f"{user_id}@example.test", created_at=created_at))
        return user_id

    def subscription(self, user_id, status="active", provider="google_play", *, end=None, start=None):
        self.session.add(Subscription(
            user_id=user_id, plan_key="cultivador", period="monthly", kind="recurring", provider=provider,
            status=status, current_period_start=start, current_period_end=end if end is not None else NOW + timedelta(days=1),
        ))

    def purchase(self, paid_at, status="paid"):
        self.session.add(CreditPurchase(
            label="Pacote de teste", credits=10, price_brl=5, provider="mercadopago", status=status, paid_at=paid_at,
        ))

    def stats(self, days):
        self.session.commit()
        result = self.client.get(URL, headers=HEADERS, params={"days": days})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_intervals_use_sao_paulo_and_compare_same_local_cutoff(self):
        for timestamp in (
            "2026-10-28 02:59:59", "2026-10-28 03:00:00",
            "2026-11-03 14:29:59", "2026-11-03 14:30:00", "2026-11-04 02:59:59",
            "2026-11-04 03:00:00", "2026-11-10 02:59:59", "2026-11-10 14:29:59",
            "2026-11-10 14:30:00",
        ):
            self.user(utc(timestamp))
        self.purchase(utc("2026-11-03 14:29:59"))
        self.purchase(utc("2026-11-03 14:30:00"))
        self.purchase(utc("2026-11-10 02:59:59"))
        for status in ("pending", "failed", "refunded", "charged_back"):
            self.purchase(utc("2026-11-10 02:59:59"), status)
        self.purchase(None)

        stats = self.stats(7)
        self.assertEqual(stats["current"], {
            "start_date": "2026-11-04", "end_date": "2026-11-10", "new_users": 3, "paid_credit_purchases": 1,
        })
        self.assertEqual(stats["previous"], {
            "start_date": "2026-10-28", "end_date": "2026-11-03", "new_users": 2, "paid_credit_purchases": 1,
        })
        self.assertEqual([day["new_users"] for day in stats["daily"]], [1, 0, 0, 0, 0, 1, 1])
        self.assertEqual([day["paid_credit_purchases"] for day in stats["daily"]], [0, 0, 0, 0, 0, 1, 0])

    def test_active_subscribers_count_each_account_with_current_access_once(self):
        old = NOW - timedelta(days=100)
        account = self.user(old)
        self.subscription(account)
        self.subscription(account, provider="mercadopago")
        self.subscription(self.user(old), status="canceled")
        self.subscription(self.user(old), provider="mercadopago", end=NOW - timedelta(days=1))
        self.subscription(self.user(old), end=NOW - timedelta(days=1))
        self.subscription(self.user(old), status="pending")
        self.subscription(self.user(old), start=NOW + timedelta(days=1))
        self.subscription(None)

        for days in (7, 90):
            stats = self.stats(days)
            self.assertEqual(stats["active_subscribers"], 3)
            self.assertEqual(stats["current"]["new_users"], 0)
            self.assertEqual(len(stats["daily"]), days)

    def test_requires_admin_token_and_valid_period(self):
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.client.get(URL, headers=HEADERS, params={"days": "12"}).status_code, 422)
        result = self.client.get(URL, headers=HEADERS)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()["daily"]), 30)
