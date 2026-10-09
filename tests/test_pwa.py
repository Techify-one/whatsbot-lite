"""Isolated PWA contract tests; no GOWA, database or application startup needed.

Run: python -m unittest discover -s tests -p test_pwa.py
"""
import ast
import importlib.util
from pathlib import Path
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
# Load the standalone helper without server/__init__.py importing the full app.
_spec = importlib.util.spec_from_file_location("pwa_routes", ROOT / "server/pwa.py")
_pwa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pwa)
register_pwa_routes = _pwa.register_pwa_routes


class PwaRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        register_pwa_routes(app, ROOT / "web")
        cls.client = TestClient(app)

    def test_public_resource_routes_and_headers(self):
        for path, mime in (
            ("/sw.js", "application/javascript"),
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/offline.html", "text/html"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith(mime))
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.get("/sw.js").headers["service-worker-allowed"], "/")

    def test_manifest_contract(self):
        manifest = self.client.get("/manifest.webmanifest").json()
        for key in ("id", "scope", "start_url"):
            self.assertEqual(manifest[key], "/")
        self.assertEqual(manifest["lang"], "pt-BR")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})
        self.assertTrue(any(icon["purpose"] == "maskable" for icon in manifest["icons"]))
        for icon in manifest["icons"]:
            self.assertEqual(icon["type"], "image/png")
            self.assertTrue((ROOT / "web" / icon["src"].lstrip("/")).is_file())

    def test_offline_page_is_self_contained(self):
        html = self.client.get("/offline.html").text
        self.assertIn('lang="pt-BR"', html)
        self.assertIn("Tentar novamente", html)
        self.assertNotIn('src="', html)
        self.assertNotIn('href="', html)
        self.assertNotIn("localStorage", html)

    def test_production_factory_registers_routes(self):
        # Integration seam check without importing DB/LLM modules or spawning GOWA.
        module = ast.parse((ROOT / "server/app.py").read_text())
        calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "register_pwa_routes"]
        self.assertEqual(len(calls), 1)
        self.assertEqual([arg.id for arg in calls[0].args], ["app", "web_dir"])


if __name__ == "__main__":
    unittest.main()
