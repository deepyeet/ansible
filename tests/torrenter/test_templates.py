"""Validate the deployment contracts without production inventory secrets."""

import json
import tomllib
import unittest
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "roles/torrenter/templates"


class TemplateTests(unittest.TestCase):
    def render(self, name, **variables):
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters["to_json"] = json.dumps
        return environment.from_string((TEMPLATES / name).read_text()).render(**variables)

    def test_auth_roundtrips_dummy_special_characters(self):
        user = 'dummy"user\\with#chars'
        password = 'dummy"pass\\word#\nline'
        result = tomllib.loads(self.render("gluetun-auth.toml.j2",
                                           GLUETUN_USER=user, GLUETUN_PASS=password))
        self.assertEqual(result["roles"], [{
            "name": "transmission-monitor",
            "routes": ["GET /v1/portforward", "GET /v1/vpn/status", "PUT /v1/vpn/status"],
            "auth": "basic", "username": user, "password": password,
        }])

    def test_compose_renders_recovery_contract(self):
        variables = {
            "torrenter_docker_volumes": {"synology_backup": {"driver": "local", "driver_opts": {"type": "none"}}},
            "torrenter_gluetun_version": "v3.41.3",
            "torrenter_data_host_path": "/mnt/expansion",
            "torrenter_backup_docker_volume_name": "synology_backup",
            "healthcheck_uuid_daily_data_backup": "dummy",
            "torrenter_check_interval_seconds": 300,
            "torrenter_port_zero_restart_threshold": 3,
            "torrenter_closed_port_restart_threshold": 3,
            "torrenter_recovery_grace_seconds": 180,
            "torrenter_vpn_recovery_timeout_seconds": 180,
            "torrenter_vpn_recovery_cooldown_seconds": 1800,
            "torrenter_recovery_enabled": True,
        }
        content = self.render("compose.yaml.j2", **variables)
        compose = yaml.safe_load(content)
        services = compose["services"]
        self.assertEqual(services["gluetun"]["image"], "qmcgaw/gluetun:v3.41.3")
        self.assertIn("/gluetun/update-port.sh {{PORT}}", content)
        self.assertIn("./gluetun/auth/config.toml:/gluetun/auth/config.toml:ro",
                      services["gluetun"]["volumes"])
        monitor = services["transmission-healthcheck"]
        self.assertEqual(monitor["depends_on"]["gluetun"]["condition"], "service_started")
        self.assertEqual(monitor["depends_on"]["transmission"]["condition"], "service_started")
        self.assertIn("./recovery-state:/state", monitor["volumes"])
        self.assertEqual(services["backup"]["profiles"], ["backup"])


if __name__ == "__main__":
    unittest.main()
