"""Proteção da API (fase 1): limite de requisições, IP real atrás do proxy e nada interno nas respostas."""
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import test_private_access as support
from app.config import settings
from app.main import create_app
from app.services import rate_limit
from app.services.auth import create_access_token


class RedisDown:
    """Sem Redis nos testes: o limitador cai para o contador em memória."""

    async def incr(self, key):
        raise ConnectionError("sem redis")


class LimitTests(unittest.TestCase):
    def setUp(self):
        rate_limit.memory.values.clear()
        self.enterContext(patch("app.services.redis_client._get_redis", return_value=RedisDown()))
        self.enterContext(patch.object(settings, "RATE_LIMIT_ENABLED", True))
        # relógio parado no meio de uma janela: as chamadas do teste nunca atravessam a virada
        self.enterContext(patch.object(rate_limit.time, "time", return_value=1_800_000_030.0))
        self.client = TestClient(create_app(), raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_auth_routes_are_limited_by_ip_with_retry_after(self):
        with patch.object(settings, "RATE_LIMIT_AUTH_IP", "2/60"):
            codes = [self.client.post("/api/v1/auth/login", json={}).status_code for _ in range(3)]
            self.assertEqual(codes, [422, 422, 429])
            blocked = self.client.post("/api/v1/auth/forgot-password", json={})
        self.assertEqual(blocked.status_code, 429)
        self.assertTrue(1 <= int(blocked.headers["retry-after"]) <= 60)
        self.assertIn("Muitas requisições", blocked.json()["detail"])

    def test_chat_is_limited_per_user_not_per_ip(self):
        alice, bob = create_access_token(uuid.uuid4()), create_access_token(uuid.uuid4())
        with patch.object(settings, "RATE_LIMIT_CHAT_USER", "2/60"):
            send = lambda token: self.client.post("/api/v1/ai/chat", json={},
                                                  headers={"Authorization": f"Bearer {token}"}).status_code
            # sem banco neste teste a rota falha depois do limitador; o que importa é quando vira 429
            self.assertEqual([send(alice) == 429 for _ in range(3)], [False, False, True])
            self.assertNotEqual(send(bob), 429)  # outro usuário, mesmo IP: segue

    def test_other_groups_and_the_general_ceiling(self):
        token = create_access_token(uuid.uuid4())
        auth = {"Authorization": f"Bearer {token}"}
        with patch.object(settings, "RATE_LIMIT_SHOP_CLICK_USER", "1/60"):
            click = f"/api/v1/shop/products/{uuid.uuid4()}/click"
            self.assertNotEqual(self.client.post(click, headers=auth).status_code, 429)
            self.assertEqual(self.client.post(click, headers=auth).status_code, 429)
        with patch.object(settings, "RATE_LIMIT_PUBLIC_IP", "1/60"):
            self.assertNotEqual(self.client.get("/api/v1/billing/catalog").status_code, 429)
            self.assertEqual(self.client.get("/api/v1/billing/catalog").status_code, 429)

    def test_logged_in_requests_do_not_count_against_the_shared_ip(self):
        # o BFF do web fala com a API de um IP só: o teto por IP não pode barrar quem está logado
        with patch.object(settings, "RATE_LIMIT_DEFAULT_IP", "1/60"), patch.object(settings, "RATE_LIMIT_CHAT_IP", "1/60"):
            for _ in range(3):
                token = create_access_token(uuid.uuid4())
                status = self.client.post("/api/v1/ai/chat", json={}, headers={"Authorization": f"Bearer {token}"}).status_code
                self.assertNotEqual(status, 429)

    def test_health_admin_and_webhooks_are_not_limited(self):
        with patch.object(settings, "RATE_LIMIT_PUBLIC_IP", "1/60"), patch.object(settings, "RATE_LIMIT_DEFAULT_IP", "1/60"):
            for path in ("/health", "/api/v1/admin/stats", "/api/v1/billing/webhooks/mercadopago"):
                codes = {self.client.get(path).status_code for _ in range(3)}
                self.assertNotIn(429, codes, path)

    def test_switch_off(self):
        with patch.object(settings, "RATE_LIMIT_ENABLED", False), patch.object(settings, "RATE_LIMIT_AUTH_IP", "1/60"):
            codes = {self.client.post("/api/v1/auth/login", json={}).status_code for _ in range(3)}
        self.assertEqual(codes, {422})


class ClientIpTests(unittest.TestCase):
    def test_forwarded_for_only_from_a_trusted_proxy(self):
        self.assertEqual(rate_limit.client_ip("203.0.113.9", "1.2.3.4"), "203.0.113.9")  # direto: ignora o cabeçalho
        self.assertEqual(rate_limit.client_ip("172.18.0.2", "1.2.3.4"), "1.2.3.4")  # Traefik na rede do Docker
        # o cliente forjou um X-Forwarded-For: vale o último endereço de fora antes do proxy
        self.assertEqual(rate_limit.client_ip("172.18.0.2", "9.9.9.9, 1.2.3.4, 10.0.0.5"), "1.2.3.4")
        self.assertEqual(rate_limit.client_ip("172.18.0.2", None), "172.18.0.2")


class BffClientIpTests(unittest.TestCase):
    """O BFF do web manda o IP do navegador assinado (o Traefik descarta o XFF que vem de fora)."""

    SECRET = "segredo-do-bff-de-teste"

    def ip(self, key, header, peer="203.0.113.50", xff=None):
        return rate_limit.client_ip(peer, xff, key, header)

    def test_right_secret_uses_the_browser_ip(self):
        with patch.object(settings, "BFF_SHARED_SECRET", self.SECRET):
            self.assertEqual(self.ip(self.SECRET, "198.51.100.7"), "198.51.100.7")
            self.assertEqual(self.ip(self.SECRET, " 2001:db8::1 "), "2001:db8::1")

    def test_wrong_secret_is_ignored_and_never_logged(self):
        with patch.object(settings, "BFF_SHARED_SECRET", self.SECRET), \
                self.assertLogs("app.services.rate_limit", level="WARNING") as logs:
            self.assertEqual(self.ip("chute", "198.51.100.7"), "203.0.113.50")
        self.assertNotIn(self.SECRET, "\n".join(logs.output))
        self.assertNotIn("chute", "\n".join(logs.output))

    def test_empty_secret_turns_the_header_off(self):
        with patch.object(settings, "BFF_SHARED_SECRET", ""):
            self.assertEqual(self.ip("", "198.51.100.7"), "203.0.113.50")
            self.assertEqual(self.ip("qualquer", "198.51.100.7"), "203.0.113.50")

    def test_invalid_ip_falls_back_to_the_proxy_rule(self):
        with patch.object(settings, "BFF_SHARED_SECRET", self.SECRET), self.assertLogs("app.services.rate_limit", "WARNING"):
            self.assertEqual(self.ip(self.SECRET, "999.1.1.1", peer="172.18.0.2", xff="1.2.3.4"), "1.2.3.4")

    def test_the_limiter_counts_each_browser_separately(self):
        rate_limit.memory.values.clear()
        client = TestClient(create_app(), raise_server_exceptions=False)
        login = lambda ip: client.post("/api/v1/auth/login", json={}, headers={
            "X-LetsGrow-BFF-Key": self.SECRET, "X-LetsGrow-Client-IP": ip}).status_code
        with patch("app.services.redis_client._get_redis", return_value=RedisDown()), \
                patch.object(rate_limit.time, "time", return_value=1_800_000_030.0), \
                patch.object(settings, "BFF_SHARED_SECRET", self.SECRET), \
                patch.object(settings, "RATE_LIMIT_AUTH_IP", "1/60"):
            self.assertEqual([login("198.51.100.1"), login("198.51.100.1")], [422, 429])
            self.assertEqual(login("198.51.100.2"), 422)  # outro navegador atrás do mesmo BFF


class ResponseTests(unittest.TestCase):
    def test_api_docs_are_off_by_default(self):
        client = TestClient(create_app())
        for path in ("/api-docs", "/api-redoc", "/api-docs/openapi.json"):
            self.assertEqual(client.get(path).status_code, 404, path)
        with patch.object(settings, "API_DOCS_ENABLED", True):
            self.assertEqual(TestClient(create_app()).get("/api-docs/openapi.json").status_code, 200)

    def test_old_public_ai_panel_is_gone(self):
        self.assertEqual(TestClient(create_app()).get("/admin/ai-panel").status_code, 404)

    def test_no_server_header_in_start_script(self):
        from pathlib import Path
        start = (Path(__file__).resolve().parents[1] / "start.sh").read_text(encoding="utf-8")
        self.assertIn("--no-server-header", start)

    def test_unhandled_error_is_generic(self):
        app = create_app()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("gemini-2.5-flash: 429 quota exceeded for key AIza-segredo")

        response = TestClient(app, raise_server_exceptions=False).get("/boom")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Erro interno. Tente de novo em instantes."})
        self.assertNotIn("gemini", response.text)
        self.assertNotIn("Traceback", response.text)

    def test_strain_match_hides_the_internal_file(self):
        from types import SimpleNamespace
        from app.schemas.strain import StrainResponse
        row = SimpleNamespace(id=uuid.uuid4(), name="AK-47", slug="ak-47", aliases=None, strain_type="photo",
                              genetics="hybrid", breeder=None, thc_pct="20%", cbd_pct="<0.2%", dominant_terpene=None,
                              flowering_days=60, height_cm=None, summary=None, image_url=None,
                              source_file="strains/ak-47.md")
        self.assertIsNone(StrainResponse.model_validate(row).model_dump()["source_file"])

    def test_knowledge_source_id_is_opaque(self):
        from app.services.chat_sources import opaque_id
        chunk = uuid.uuid4()
        self.assertEqual(opaque_id(chunk), opaque_id(chunk))  # estável
        self.assertTrue(opaque_id(chunk).startswith("kb-"))
        self.assertNotIn(str(chunk), opaque_id(chunk))


class PhotoAnalysisErrorTests(unittest.TestCase):
    def setUp(self):
        self.f = support.PrivateAccessTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)

    def test_provider_error_does_not_reach_the_app(self):
        f = self.f
        f.event.photo_keys = [f.own_key]
        f.session.commit()
        with patch("app.services.rag.analyze_event_photos",
                   new=AsyncMock(side_effect=RuntimeError("gemini-2.5-flash: 429 RESOURCE_EXHAUSTED"))), \
                patch("app.api.v1.credits.start_paid_task", new=AsyncMock(return_value=None)), \
                patch("app.api.v1.plants.logger"):
            response = f.client.post(f"/plants/{f.plant}/events/{f.event.id}/analyze")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn("gemini", response.text)
        self.assertIn("Não foi possível analisar as fotos agora", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
