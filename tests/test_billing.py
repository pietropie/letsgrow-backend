"""Pagamentos: Mercado Pago (webapp) e Google Play (Android) com os provedores simulados.

Rotas reais em SQLite em memoria. Nenhuma chamada sai para o Mercado Pago ou o Google:
as funcoes de cliente (services/mercadopago.py e services/google_play.py) sao trocadas
por AsyncMock, e a assinatura do webhook do MP e calculada com um secret de teste.
"""
import base64
import hashlib
import hmac
import json
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import test_private_access as support  # registra JSONB -> JSON no SQLite
from app.api.v1 import admin_billing, admin_business, billing as billing_api, credits
from app.config import settings
from app.database import get_db
from app.models.app_settings import AppSettings
from app.models.base import Base
from app.models.billing import PaymentEvent, PlanPrice, Subscription
from app.models.credits import CreditLedgerEntry, CreditPackage, CreditPurchase, CreditQuote, CreditTaskCost
from app.models.plan_config import PlanConfig
from app.models.user import User
from app.services import billing, google_play as gp, mercadopago as mp, plan_config_service
from app.services.app_settings import get_app_settings
from app.services.auth import get_current_user
from test_sem_api import Db, run

TABLES = [
    User, AppSettings, PlanConfig, CreditTaskCost, CreditPackage, CreditQuote, CreditPurchase, CreditLedgerEntry,
    PlanPrice, Subscription, PaymentEvent,
]
SECRET = 'segredo-de-teste'


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


class BillingBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})

        @event.listens_for(self.engine, 'connect')
        def _fk(dbapi_conn, _):
            dbapi_conn.execute('PRAGMA foreign_keys=ON')

        Base.metadata.create_all(self.engine, tables=[m.__table__ for m in TABLES])
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = Db(self.session)
        self.now = datetime.now(timezone.utc)
        self.owner = User(id=uuid.uuid4(), email='dona@example.test', username='dona', plan='free')
        self.other = User(id=uuid.uuid4(), email='outra@example.test', username='outra', plan='free')
        self.session.add_all([self.owner, self.other])
        self.session.commit()

        self.current = self.owner
        app = FastAPI()
        app.include_router(billing_api.router, prefix='/billing')
        app.include_router(credits.router, prefix='/credits')
        app.include_router(admin_billing.router, prefix='/admin')
        app.include_router(admin_business.router, prefix='/admin')
        app.dependency_overrides[get_current_user] = lambda: self.current
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        plan_config_service._invalidate()
        self.addCleanup(plan_config_service._invalidate)
        self.settings_row = run(get_app_settings(self.db))
        for name, value in {
            'ADMIN_TOKEN': 'token-de-teste', 'MP_ACCESS_TOKEN': 'TEST-token', 'MP_WEBHOOK_SECRET': SECRET,
            'GOOGLE_PLAY_SERVICE_ACCOUNT_JSON': '{"fake": true}', 'BILLING_GRACE_DAYS': 3,
            'WEB_APP_URL': 'https://web.example.test',
        }.items():
            self.enterContext(patch.object(settings, name, value))

    def tearDown(self):
        self.client.close()
        self.session.close()
        self.engine.dispose()

    def admin(self):
        return {'X-Admin-Token': 'token-de-teste'}

    def set_price(self, plan_key='cultivador', period='monthly', price=44.9, active=True,
                  product='letsgrow_cultivador', base_plan=None):
        response = self.client.put(f'/admin/billing/plan-prices/{plan_key}/{period}', headers=self.admin(), json={
            'price_brl': price, 'is_active': active, 'google_product_id': product,
            'google_base_plan_id': base_plan or (period if product else None),
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def mp_webhook(self, topic, data_id, notification_id=None, signature_ok=True):
        request_id = str(uuid.uuid4())
        ts = '1730000000'
        manifest = f'id:{data_id.lower()};request-id:{request_id};ts:{ts};'
        digest = hmac.new(SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        if not signature_ok:
            digest = '0' * 64
        return self.client.post(
            f'/billing/webhooks/mercadopago?type={topic}&data.id={data_id}',
            json={'id': notification_id or str(uuid.uuid4()), 'type': topic, 'data': {'id': data_id}},
            headers={'x-signature': f'ts={ts},v1={digest}', 'x-request-id': request_id},
        )

    def preapproval(self, sub, status='authorized', charged=1, last_charged=None):
        return {
            'id': sub.provider_ref, 'external_reference': f'sub:{sub.id}', 'status': status,
            'summarized': {'charged_quantity': charged,
                           'last_charged_date': iso(last_charged or self.now) if charged else None},
        }

    def reload(self, obj):
        self.session.refresh(obj)
        return obj

    def subscribe_card(self, plan_key='cultivador', period='monthly', preapproval_id='pa-1'):
        create = AsyncMock(return_value={'id': preapproval_id, 'init_point': 'https://mp.example.test/pa'})
        with patch.object(mp, 'create_preapproval', create):
            response = self.client.post('/billing/checkout', json={
                'kind': 'subscription', 'plan_key': plan_key, 'period': period, 'method': 'card'})
        self.assertEqual(response.status_code, 201, response.text)
        return self.session.get(Subscription, uuid.UUID(response.json()['id'])), create

    def activate(self, sub, last_charged=None):
        with patch.object(mp, 'get_preapproval', AsyncMock(return_value=self.preapproval(sub, last_charged=last_charged))), \
             patch.object(mp, 'cancel_preapproval', AsyncMock(return_value={})) as cancel:
            response = self.mp_webhook('subscription_preapproval', sub.provider_ref)
        self.assertEqual(response.status_code, 204, response.text)
        return cancel


class CatalogAndAdminTests(BillingBase):
    def test_nothing_is_for_sale_until_admin_sets_and_activates_a_price(self):
        self.assertEqual(self.client.get('/billing/catalog').json()['plans'], [])
        rows = self.client.get('/admin/billing/plan-prices', headers=self.admin()).json()
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r['price_brl'] is None and not r['is_active'] for r in rows))

        response = self.client.put('/admin/billing/plan-prices/cultivador/monthly', headers=self.admin(),
                                   json={'is_active': True})
        self.assertEqual(response.status_code, 422)
        self.set_price(active=False)
        self.assertEqual(self.client.get('/billing/catalog').json()['plans'], [])
        self.set_price()
        catalog = self.client.get('/billing/catalog').json()
        self.assertEqual(catalog['plans'][0]['plan_key'], 'cultivador')
        self.assertEqual(catalog['plans'][0]['periods'][0]['google_base_plan_id'], 'monthly')
        self.assertTrue(catalog['web_enabled'] and catalog['android_enabled'])

    def test_admin_validates_play_ids_and_requires_token(self):
        self.assertEqual(self.client.get('/admin/billing/plan-prices').status_code, 403)
        response = self.client.put('/admin/billing/plan-prices/cultivador/monthly', headers=self.admin(),
                                   json={'price_brl': 10, 'google_product_id': 'x'})
        self.assertEqual(response.status_code, 422)
        self.set_price('cultivador', 'monthly', product='p', base_plan='b')
        response = self.client.put('/admin/billing/plan-prices/grower_pro/monthly', headers=self.admin(),
                                   json={'price_brl': 10, 'google_product_id': 'p', 'google_base_plan_id': 'b'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.put('/admin/billing/plan-prices/free/monthly', headers=self.admin(),
                                         json={'price_brl': 1}).status_code, 404)
        package = {'label': 'P', 'credits': 10, 'price_brl': 5, 'google_product_id': 'letsgrow_creditos_10'}
        self.assertEqual(self.client.post('/admin/credits/packages', headers=self.admin(), json=package).status_code, 201)
        self.assertEqual(self.client.post('/admin/credits/packages', headers=self.admin(), json=package).status_code, 409)
        status = self.client.get('/admin/billing/status', headers=self.admin()).json()
        self.assertTrue(status['mercadopago_sandbox'])
        self.assertNotIn('TEST-token', json.dumps(status))


class MercadoPagoSubscriptionTests(BillingBase):
    def setUp(self):
        super().setUp()
        self.set_price('jardineiro', 'monthly', 24.9, product=None)
        self.set_price('cultivador', 'monthly', 44.9, product=None)
        self.set_price('cultivador', 'yearly', 449.0, product=None)

    def test_checkout_requires_price_and_configured_provider(self):
        response = self.client.post('/billing/checkout', json={
            'kind': 'subscription', 'plan_key': 'grower_pro', 'period': 'monthly', 'method': 'card'})
        self.assertEqual(response.json()['detail']['code'], 'plan_not_for_sale')
        with patch.object(settings, 'MP_ACCESS_TOKEN', ''):
            response = self.client.post('/billing/checkout', json={
                'kind': 'subscription', 'plan_key': 'cultivador', 'period': 'monthly', 'method': 'card'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.session.scalars(select(Subscription)).all(), [])

    def test_card_subscription_activates_only_after_first_charge_and_webhook_is_idempotent(self):
        sub, create = self.subscribe_card()
        kwargs = create.call_args.kwargs
        self.assertEqual((kwargs['amount'], kwargs['months'], kwargs['start_date']), (44.9, 1, None))
        self.assertEqual(kwargs['external_reference'], f'sub:{sub.id}')
        self.assertEqual(sub.checkout_url, 'https://mp.example.test/pa')
        self.assertEqual(self.owner.plan, 'free')

        with patch.object(mp, 'get_preapproval', AsyncMock(return_value=self.preapproval(sub, charged=0))):
            self.assertEqual(self.mp_webhook('subscription_preapproval', 'pa-1').status_code, 204)
        self.assertEqual((self.reload(sub).status, sub.auto_renew), ('pending', True))
        self.assertEqual(self.owner.plan, 'free')

        charged = self.now - timedelta(hours=1)
        get = AsyncMock(return_value=self.preapproval(sub, last_charged=charged))
        with patch.object(mp, 'get_preapproval', get):
            self.assertEqual(self.mp_webhook('subscription_preapproval', 'pa-1', notification_id='n-1').status_code, 204)
            self.assertEqual(self.mp_webhook('subscription_preapproval', 'pa-1', notification_id='n-1').status_code, 204)
        self.assertEqual(get.await_count, 1)
        self.assertEqual(self.reload(sub).status, 'active')
        owner = self.owner
        self.assertEqual(owner.plan, 'cultivador')
        self.assertEqual(utc(owner.plan_expires_at), billing.add_months(charged, 1) + timedelta(days=3))

        state = self.client.get('/billing/subscription').json()
        self.assertEqual((state['effective_plan'], state['managed_by']), ('cultivador', 'web'))
        self.assertIsNone(state['current']['checkout_url'])

    def test_invalid_signature_is_rejected_without_calling_the_provider(self):
        get = AsyncMock()
        with patch.object(mp, 'get_payment', get):
            self.assertEqual(self.mp_webhook('payment', '123', signature_ok=False).status_code, 401)
            with patch.object(settings, 'MP_WEBHOOK_SECRET', ''):
                self.assertEqual(self.mp_webhook('payment', '123').status_code, 401)
        get.assert_not_awaited()

    def test_upgrade_replaces_card_subscription_and_cancel_keeps_paid_period(self):
        old, _ = self.subscribe_card('jardineiro', preapproval_id='pa-old')
        self.activate(old)
        self.assertEqual(self.owner.plan, 'jardineiro')

        new, create = self.subscribe_card('cultivador', preapproval_id='pa-new')
        self.assertIsNone(create.call_args.kwargs['start_date'])
        cancel = self.activate(new)
        cancel.assert_awaited_once_with('pa-old')
        self.assertEqual((self.reload(old).status, old.auto_renew), ('canceled', False))
        self.assertEqual(self.owner.plan, 'cultivador')

        response = self.client.post('/billing/checkout', json={
            'kind': 'subscription', 'plan_key': 'cultivador', 'period': 'monthly', 'method': 'card'})
        self.assertEqual(response.json()['detail']['code'], 'already_subscribed')

        with patch.object(mp, 'cancel_preapproval', AsyncMock(return_value={})) as cancel:
            response = self.client.post('/billing/subscription/cancel')
        self.assertEqual(response.status_code, 200, response.text)
        cancel.assert_awaited_once_with('pa-new')
        self.assertEqual((self.reload(new).status, new.auto_renew), ('canceled', False))
        self.assertEqual(self.owner.plan, 'cultivador')
        self.assertEqual(self.client.post('/billing/subscription/cancel').status_code, 404)

    def test_downgrade_and_period_change_start_when_current_period_ends(self):
        current, _ = self.subscribe_card('cultivador', preapproval_id='pa-1')
        self.activate(current)
        end = utc(self.reload(current).current_period_end)

        down, create = self.subscribe_card('jardineiro', preapproval_id='pa-2')
        self.assertEqual(create.call_args.kwargs['start_date'], end)
        self.assertEqual(utc(down.current_period_start), end)
        with patch.object(mp, 'get_preapproval', AsyncMock(return_value=self.preapproval(down, charged=0))), \
             patch.object(mp, 'cancel_preapproval', AsyncMock(return_value={})) as cancel:
            self.mp_webhook('subscription_preapproval', 'pa-2')
        cancel.assert_awaited_once_with('pa-1')
        self.assertEqual(self.owner.plan, 'cultivador')
        state = self.client.get('/billing/subscription').json()
        self.assertEqual((state['current']['plan_key'], state['upcoming']['plan_key']), ('cultivador', 'jardineiro'))

        _, yearly = self.subscribe_card('cultivador', 'yearly', preapproval_id='pa-3')
        self.assertEqual((yearly.call_args.kwargs['months'], yearly.call_args.kwargs['start_date']), (12, end))

    def test_pix_pays_one_period_in_advance_and_checks_the_amount(self):
        create = AsyncMock(return_value={'id': 'pref-1', 'init_point': 'https://mp.example.test/p',
                                         'sandbox_init_point': 'https://sandbox.mp.example.test/p'})
        with patch.object(mp, 'create_preference', create):
            response = self.client.post('/billing/checkout', json={
                'kind': 'subscription', 'plan_key': 'cultivador', 'period': 'yearly', 'method': 'pix'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['checkout_url'], 'https://sandbox.mp.example.test/p')
        self.assertTrue(create.call_args.kwargs['pix_only'])
        sub = self.session.get(Subscription, uuid.UUID(response.json()['id']))
        self.assertEqual((sub.kind, sub.auto_renew), ('prepaid', False))

        low = {'id': 900, 'status': 'approved', 'external_reference': f'period:{sub.id}', 'transaction_amount': 1.0}
        with patch.object(mp, 'get_payment', AsyncMock(return_value=low)):
            self.mp_webhook('payment', '900')
        self.assertEqual(self.reload(sub).status, 'pending')

        paid = {**low, 'id': 901, 'transaction_amount': 449.0}
        with patch.object(mp, 'get_payment', AsyncMock(return_value=paid)):
            self.mp_webhook('payment', '901')
        self.assertEqual(self.reload(sub).status, 'active')
        self.assertEqual(utc(sub.current_period_end), billing.add_months(utc(sub.current_period_start), 12))
        owner = self.owner
        self.assertEqual((owner.plan, utc(owner.plan_expires_at)), ('cultivador', utc(sub.current_period_end)))

        refunded = {**paid, 'status': 'refunded'}
        with patch.object(mp, 'get_payment', AsyncMock(return_value=refunded)):
            self.mp_webhook('payment', '901')
        self.assertEqual(self.reload(sub).status, 'refunded')
        self.assertEqual((self.owner.plan, self.owner.plan_expires_at), ('free', None))

    def test_chargeback_on_card_charge_drops_the_plan_and_stops_renewal(self):
        sub, _ = self.subscribe_card()
        self.activate(sub)
        charge = {'id': 77, 'status': 'charged_back', 'external_reference': f'sub:{sub.id}'}
        with patch.object(mp, 'get_payment', AsyncMock(return_value=charge)), \
             patch.object(mp, 'cancel_preapproval', AsyncMock(return_value={})) as cancel:
            self.mp_webhook('payment', '77')
        cancel.assert_awaited_once_with('pa-1')
        self.assertEqual((self.reload(sub).status, sub.auto_renew), ('refunded', False))
        self.assertEqual(self.owner.plan, 'free')

    def test_manual_admin_plan_is_not_touched_without_subscription(self):
        self.owner.plan = 'grower_pro'
        self.session.commit()
        run(billing.sync_user_plan(self.db, self.owner))
        self.assertEqual(self.owner.plan, 'grower_pro')

    def test_reconcile_closes_abandoned_checkouts_and_applies_missed_payment(self):
        old_purchase = CreditPurchase(id=uuid.uuid4(), user_id=self.owner.id, label='P', credits=10, price_brl=5,
                                      provider='mercadopago', provider_ref='x', status='pending',
                                      created_at=self.now - timedelta(days=4), updated_at=self.now)
        abandoned = Subscription(id=uuid.uuid4(), user_id=self.owner.id, plan_key='cultivador', period='monthly',
                                 provider='mercadopago', kind='prepaid', status='pending', price_brl=44.9,
                                 created_at=self.now - timedelta(days=4), updated_at=self.now)
        missed = Subscription(id=uuid.uuid4(), user_id=self.owner.id, plan_key='jardineiro', period='monthly',
                              provider='mercadopago', kind='prepaid', status='pending', price_brl=24.9,
                              created_at=self.now - timedelta(hours=1), updated_at=self.now)
        self.session.add_all([old_purchase, abandoned, missed])
        self.session.commit()
        search = AsyncMock(return_value=[{'id': 5, 'status': 'approved', 'external_reference': f'period:{missed.id}',
                                          'transaction_amount': 24.9}])
        with patch.object(mp, 'search_payments', search), patch.object(gp, 'is_configured', return_value=False):
            stats = run(billing.reconcile(self.db))
        self.assertEqual(stats['errors'], 0)
        self.assertEqual(self.reload(old_purchase).status, 'canceled')
        self.assertEqual(self.reload(abandoned).status, 'failed')
        self.assertEqual(self.reload(missed).status, 'active')
        self.assertEqual(self.owner.plan, 'jardineiro')


class MercadoPagoCreditsTests(BillingBase):
    def setUp(self):
        super().setUp()
        self.settings_row.credits_enabled = True
        self.package = CreditPackage(id=uuid.uuid4(), label='Pacote 100', credits=100, price_brl=19.9,
                                     validity_days=None, is_active=True)
        self.session.add(self.package)
        self.session.commit()
        self.enterContext(patch.object(settings, 'PAYMENT_PROVIDER', 'mercadopago'))

    def balance(self):
        return self.client.get('/credits/balance').json()['extra_available']

    def test_credit_checkout_payment_and_refund(self):
        create = AsyncMock(return_value={'id': 'pref', 'init_point': 'https://mp.example.test/c'})
        with patch.object(mp, 'create_preference', create):
            response = self.client.post('/billing/checkout', json={'kind': 'credits', 'package_id': str(self.package.id)})
        self.assertEqual(response.status_code, 201, response.text)
        purchase_id = response.json()['id']
        self.assertFalse(create.call_args.kwargs['pix_only'])
        self.assertEqual(create.call_args.kwargs['external_reference'], f'credits:{purchase_id}')

        payment = {'id': 1, 'status': 'approved', 'external_reference': f'credits:{purchase_id}', 'transaction_amount': 19.9}
        with patch.object(mp, 'get_payment', AsyncMock(return_value=payment)):
            self.mp_webhook('payment', '1')
            self.mp_webhook('payment', '1')
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.client.get(f'/credits/purchases/{purchase_id}').json()['status'], 'paid')

        with patch.object(mp, 'get_payment', AsyncMock(return_value={**payment, 'status': 'refunded'})):
            self.mp_webhook('payment', '1')
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.client.get(f'/credits/purchases/{purchase_id}').json()['status'], 'refunded')


class GooglePlayTests(BillingBase):
    def setUp(self):
        super().setUp()
        self.set_price('cultivador', 'monthly', 44.9, product='letsgrow_cultivador', base_plan='monthly')
        self.set_price('grower_pro', 'monthly', 89.9, product='letsgrow_grower_pro', base_plan='monthly')
        self.expiry = self.now + timedelta(days=30)

    def play_sub(self, user=None, state='SUBSCRIPTION_STATE_ACTIVE', product='letsgrow_cultivador',
                 ack='ACKNOWLEDGEMENT_STATE_PENDING', linked=None, auto_renew=True):
        data = {
            'subscriptionState': state, 'startTime': iso(self.now), 'acknowledgementState': ack,
            'latestOrderId': 'GPA.1234',
            'externalAccountIdentifiers': {'obfuscatedExternalAccountId': str((user or self.owner).id)},
            'lineItems': [{'productId': product, 'expiryTime': iso(self.expiry),
                           'offerDetails': {'basePlanId': 'monthly'},
                           'autoRenewingPlan': {'autoRenewEnabled': auto_renew}}],
        }
        if linked:
            data['linkedPurchaseToken'] = linked
        return data

    def verify(self, kind, product, token):
        return self.client.post('/billing/google/verify', json={'kind': kind, 'product_id': product, 'purchase_token': token})

    def push(self, payload, message_id):
        body = {'message': {'data': base64.b64encode(json.dumps(payload).encode()).decode(), 'messageId': message_id}}
        return self.client.post('/billing/webhooks/google-play', json=body, headers={'Authorization': 'Bearer x'})

    def test_verify_subscription_acknowledges_and_sets_the_account_plan(self):
        with patch.object(gp, 'get_subscription', AsyncMock(return_value=self.play_sub())), \
             patch.object(gp, 'acknowledge_subscription', AsyncMock()) as ack:
            response = self.verify('subscription', 'letsgrow_cultivador', 'tok-1')
        self.assertEqual(response.status_code, 200, response.text)
        ack.assert_awaited_once_with('letsgrow_cultivador', 'tok-1')
        body = response.json()['subscription']
        self.assertEqual((body['status'], body['managed_by']), ('active', 'google_play'))
        owner = self.owner
        self.assertEqual((owner.plan, utc(owner.plan_expires_at)), ('cultivador', self.expiry))

        # Plano da conta: a web ve a assinatura do Play e nao deixa assinar de novo nem cancelar por la.
        self.set_price('jardineiro', 'monthly', 24.9, product=None)
        response = self.client.post('/billing/checkout', json={
            'kind': 'subscription', 'plan_key': 'jardineiro', 'period': 'monthly', 'method': 'card'})
        self.assertEqual(response.json()['detail']['code'], 'managed_by_google_play')
        response = self.client.post('/billing/subscription/cancel')
        self.assertEqual(response.json()['detail']['code'], 'manage_in_google_play')
        self.assertEqual(self.client.get('/billing/subscription').json()['managed_by'], 'google_play')

    def test_purchase_of_another_account_or_unknown_product_is_refused(self):
        with patch.object(gp, 'get_subscription', AsyncMock(return_value=self.play_sub(user=self.other))):
            self.assertEqual(self.verify('subscription', 'letsgrow_cultivador', 'tok-x').status_code, 403)
        with patch.object(gp, 'get_subscription', AsyncMock(return_value=self.play_sub(product='desconhecido'))):
            response = self.verify('subscription', 'desconhecido', 'tok-y')
        self.assertEqual(response.json()['detail']['code'], 'unknown_product')
        self.assertEqual(self.session.scalars(select(Subscription)).all(), [])
        self.assertEqual(self.owner.plan, 'free')
        with patch.object(settings, 'GOOGLE_PLAY_SERVICE_ACCOUNT_JSON', ''):
            self.assertEqual(self.verify('subscription', 'letsgrow_cultivador', 'tok-z').status_code, 503)

    def test_upgrade_in_play_replaces_the_linked_token(self):
        with patch.object(gp, 'get_subscription', AsyncMock(return_value=self.play_sub())), \
             patch.object(gp, 'acknowledge_subscription', AsyncMock()):
            self.verify('subscription', 'letsgrow_cultivador', 'tok-1')
            with patch.object(gp, 'get_subscription', AsyncMock(
                    return_value=self.play_sub(product='letsgrow_grower_pro', linked='tok-1'))):
                self.verify('subscription', 'letsgrow_grower_pro', 'tok-2')
        old = self.session.scalars(select(Subscription).where(Subscription.provider_ref == 'tok-1')).one()
        self.assertEqual(old.status, 'replaced')
        self.assertEqual(self.owner.plan, 'grower_pro')

    def test_rtdn_requires_auth_is_idempotent_and_revocation_drops_the_plan(self):
        with patch.object(gp, 'get_subscription', AsyncMock(return_value=self.play_sub(ack='ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED'))):
            self.verify('subscription', 'letsgrow_cultivador', 'tok-1')
        payload = {'packageName': settings.GOOGLE_PLAY_PACKAGE_NAME,
                   'subscriptionNotification': {'notificationType': 12, 'purchaseToken': 'tok-1'}}
        with patch.object(gp, 'verify_pubsub_push', AsyncMock(return_value=False)):
            self.assertEqual(self.push(payload, 'm-1').status_code, 401)
        get = AsyncMock(return_value=self.play_sub(state='SUBSCRIPTION_STATE_EXPIRED', auto_renew=False))
        with patch.object(gp, 'verify_pubsub_push', AsyncMock(return_value=True)), patch.object(gp, 'get_subscription', get):
            self.assertEqual(self.push(payload, 'm-1').status_code, 204)
            self.assertEqual(self.push(payload, 'm-1').status_code, 204)
        self.assertEqual(get.await_count, 1)
        sub = self.session.scalars(select(Subscription)).one()
        self.assertEqual(self.reload(sub).status, 'refunded')
        self.assertEqual(self.owner.plan, 'free')

    def test_canceled_in_play_keeps_access_until_expiry_then_grace_and_hold(self):
        with patch.object(gp, 'get_subscription', AsyncMock(return_value=self.play_sub(
                state='SUBSCRIPTION_STATE_CANCELED', ack='ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED', auto_renew=False))):
            self.verify('subscription', 'letsgrow_cultivador', 'tok-1')
        self.assertEqual(self.owner.plan, 'cultivador')
        payload = {'subscriptionNotification': {'notificationType': 5, 'purchaseToken': 'tok-1'}}
        with patch.object(gp, 'verify_pubsub_push', AsyncMock(return_value=True)), \
             patch.object(gp, 'get_subscription', AsyncMock(return_value=self.play_sub(
                 state='SUBSCRIPTION_STATE_ON_HOLD', ack='ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED'))):
            self.push(payload, 'm-hold')
        self.assertEqual(self.owner.plan, 'free')

    def test_credits_are_granted_once_consumed_and_voided_refund_removes_them(self):
        self.settings_row.credits_enabled = True
        package = CreditPackage(id=uuid.uuid4(), label='Pacote 50', credits=50, price_brl=9.9, is_active=True,
                                google_product_id='letsgrow_creditos_50')
        self.session.add(package)
        self.session.commit()
        self.assertEqual(self.client.get('/credits/packages').json()[0]['google_product_id'], 'letsgrow_creditos_50')
        product = {'purchaseState': 0, 'consumptionState': 0, 'orderId': 'GPA.9',
                   'obfuscatedExternalAccountId': str(self.owner.id)}
        with patch.object(gp, 'get_product_purchase', AsyncMock(return_value=product)), \
             patch.object(gp, 'consume_product', AsyncMock()) as consume:
            first = self.verify('credits', 'letsgrow_creditos_50', 'ptok-1')
            second = self.verify('credits', 'letsgrow_creditos_50', 'ptok-1')
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.json()['purchase']['id'], first.json()['purchase']['id'])
        self.assertEqual(consume.await_count, 2)
        self.assertEqual(self.client.get('/credits/balance').json()['extra_available'], 50)

        voided = {'voidedPurchaseNotification': {'purchaseToken': 'ptok-1', 'productType': 2, 'refundType': 1}}
        with patch.object(gp, 'verify_pubsub_push', AsyncMock(return_value=True)):
            self.assertEqual(self.push(voided, 'm-void').status_code, 204)
        self.assertEqual(self.client.get('/credits/balance').json()['extra_available'], 0)


class SignatureTests(unittest.TestCase):
    def test_manifest_follows_mercado_pago_format(self):
        with patch.object(settings, 'MP_WEBHOOK_SECRET', SECRET):
            manifest = 'id:abc123;request-id:req-1;ts:1700;'
            v1 = hmac.new(SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
            self.assertTrue(mp.verify_signature(f'ts=1700,v1={v1}', 'req-1', 'ABC123'))
            self.assertFalse(mp.verify_signature(f'ts=1701,v1={v1}', 'req-1', 'ABC123'))
            self.assertFalse(mp.verify_signature(None, 'req-1', 'ABC123'))

    def test_back_urls_point_to_the_webapp_return_pages(self):
        request = AsyncMock(return_value={'id': 'pref'})
        with patch.object(settings, 'WEB_APP_URL', 'https://app.example.test/'), \
             patch.object(settings, 'MP_ACCESS_TOKEN', 'TEST-x'), patch.object(mp, '_request', request):
            run(mp.create_preference(external_reference='credits:1', title='t', amount=10, payer_email='a@b.c',
                                     kind='credits', ref_id='abc', pix_only=False))
            run(mp.create_preapproval(external_reference='sub:1', reason='r', amount=10, months=1,
                                      payer_email='a@b.c', ref_id='def', start_date=None))
        preference, preapproval = (c.kwargs['json'] for c in request.call_args_list)
        self.assertEqual(set(preference['back_urls'].values()), {'https://app.example.test/conta/creditos/retorno?purchase_id=abc'})
        self.assertEqual(preference['auto_return'], 'approved')
        self.assertEqual(preapproval['back_url'], 'https://app.example.test/conta/planos/retorno?subscription_id=def')

    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(billing.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1).day, 28)
        self.assertEqual(billing.add_months(datetime(2026, 11, 15, tzinfo=timezone.utc), 12).year, 2027)


if __name__ == '__main__':
    unittest.main()
