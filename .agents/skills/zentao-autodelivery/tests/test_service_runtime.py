import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backend_deploy
import service_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_jar_resolution_is_unique_inside_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec = {'argv': ['java', '-jar', '{jar}'], 'jar': 'target/*.jar', 'env': {'MODE': 'fixture'}}
            (root / 'target').mkdir()
            jar = root / 'target' / 'app 1.jar'
            jar.touch()
            (root / 'target/app-sources.jar').touch()
            argv, env = runtime.startup(spec, root)
            self.assertEqual(argv, ['java', '-jar', str(jar.resolve())])
            self.assertEqual(env['MODE'], 'fixture')
            (root / 'target/old.jar').touch()
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                runtime.startup(spec, root)
            with self.assertRaisesRegex(ValueError, 'relative'):
                runtime.startup({**spec, 'jar': '../*.jar'}, root)

    def test_cli_starts_configured_candidate_with_literal_arguments_and_environment(self):
        script = Path(runtime.__file__)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkout = root / 'candidate with spaces'
            checkout.mkdir()
            service = {'start': {'argv': [sys.executable, 'app.py', 'literal & argument'],
                                 'env': {'RUN_MARKER': 'fixture'}}}
            (checkout / 'app.py').write_text(
                'import os, sys; print(os.environ["RUN_MARKER"], sys.argv[1])', encoding='utf-8')
            config = root / 'project.json'
            config.write_text(json.dumps({'development': {'backend_bundle': {'services': {'test': service}}}}),
                              encoding='utf-8')
            result = subprocess.run([sys.executable, '-B', str(script), 'start', '--service', 'test',
                                     '--config', str(config), '--workspace', str(checkout)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), 'fixture literal & argument')

    def test_portable_http_check_observes_real_service_and_rejects_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == '/api?check=yes' else 302)
                self.end_headers()
            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}'
                self.assertEqual(runtime.check_http({'url': url + '/api?check=yes'})['status'], 200)
                with self.assertRaisesRegex(ValueError, '302'):
                    runtime.check_http({'url': url + '/redirect'})
            finally:
                server.shutdown()
                thread.join()

    def test_named_probe_credential_is_bound_to_https_origin_and_account(self):
        target = {'gateway_url': 'https://release.invalid', 'verification_path': '/version',
                  'http_credential_ref': 'release', 'http_username': 'probe'}
        with tempfile.TemporaryDirectory() as folder, patch.object(backend_deploy, 'LOCAL_DIRECTORY', Path(folder)):
            secret = {'username': 'probe', 'base_url': 'https://other.invalid', 'password': 'fixture'}
            config = Path(folder) / 'backend-credentials.json'
            config.write_text(json.dumps({'release': secret}), encoding='utf-8')
            with patch.object(backend_deploy, 'HTTPSConnection') as connection:
                with self.assertRaisesRegex(backend_deploy.DeploymentError, 'credential unavailable'):
                    backend_deploy.verify_http(target, 'a' * 40)
                connection.assert_not_called()
                secret['base_url'] = target['gateway_url']
                config.write_text(json.dumps({'release': secret}), encoding='utf-8')
                response = connection.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = json.dumps({'git': {'commit': {'id': 'a' * 40}}}).encode()
                proof = backend_deploy.verify_http(target, 'a' * 40)
                self.assertEqual(proof['sha'], 'a' * 40)
                self.assertIn('Authorization', connection.return_value.request.call_args.kwargs['headers'])
