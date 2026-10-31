"""POST /auth/login com conta sem senha local (criada pelo Google): 401 genérico, não 500."""
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_sem_api as sem
from app.api.v1 import auth
from app.database import get_db
from app.models.user import User
from app.services.auth import hash_password, verify_password


class PasswordlessLoginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sem.Base_()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture.session.add_all([
            User(id=uuid.uuid4(), email="google@letsgrow-teste.com.br", username="google", plan="free",
                 hashed_password=None, oauth_provider="google", oauth_id="g-1", is_verified=True),
            User(id=uuid.uuid4(), email="senha@letsgrow-teste.com.br", username="senha", plan="free",
                 hashed_password=hash_password("senha-segura-1"), is_verified=True),
        ])
        self.fixture.session.commit()
        app = FastAPI()
        app.include_router(auth.router, prefix="/auth")
        app.dependency_overrides[get_db] = lambda: self.fixture.db
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def login(self, email, password="senha-segura-1"):
        return self.client.post("/auth/login", json={"email": email, "password": password})

    def test_google_account_gets_the_same_401_as_a_wrong_password(self):
        passwordless = self.login("google@letsgrow-teste.com.br")
        wrong = self.login("senha@letsgrow-teste.com.br", "outra-senha-1")
        missing = self.login("ninguem@letsgrow-teste.com.br")
        for response in (passwordless, wrong, missing):
            self.assertEqual(response.status_code, 401, response.text)
        # mesma resposta: não revela que a conta existe nem como foi criada
        self.assertEqual(passwordless.json(), wrong.json())
        self.assertEqual(passwordless.json(), missing.json())

    def test_password_account_still_logs_in(self):
        response = self.login("senha@letsgrow-teste.com.br")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("access_token", response.json())

    def test_verify_password_without_or_with_broken_hash(self):
        self.assertFalse(verify_password("x", None))
        self.assertFalse(verify_password("x", ""))
        self.assertFalse(verify_password("x", "não-é-bcrypt"))
        self.assertTrue(verify_password("x", hash_password("x")))


if __name__ == "__main__":
    unittest.main()
