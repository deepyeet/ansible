"""Unit tests for the pixel_tailscale role templates and configuration."""
import os
from pathlib import Path
import unittest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

ROOT = Path(__file__).resolve().parent.parent


def read_yaml(rel_path):
    return yaml.safe_load((ROOT / rel_path).read_text())


def render_template(rel_path, variables):
    template_src = (ROOT / rel_path).read_text()
    return Templar(loader=DataLoader(), variables=variables).template(
        template_src, preserve_trailing_newlines=True
    )


class PixelTailscaleTemplateTests(unittest.TestCase):
    def setUp(self):
        self.defaults = read_yaml("roles/pixel_tailscale/defaults/main.yml")

    def test_defaults_structure(self):
        required_keys = [
            "pixel_tailscale_version",
            "pixel_tailscale_tarball_checksum",
            "pixel_tailscale_daemon_checksum",
            "pixel_tailscale_cli_checksum",
            "pixel_tailscale_base_dir",
            "pixel_tailscale_bin_dir",
            "pixel_tailscale_state_dir",
            "pixel_tailscale_run_dir",
            "pixel_tailscale_log_path",
            "pixel_tailscale_socket",
            "pixel_tailscale_pid_file",
            "pixel_tailscale_tun_mode",
            "pixel_tailscale_ssh_enabled",
            "pixel_tailscale_hostname",
            "pixel_tailscale_tags",
        ]
        for key in required_keys:
            self.assertIn(key, self.defaults, f"Missing default variable: {key}")

    def test_render_tailscaled_service_script(self):
        script = render_template(
            "roles/pixel_tailscale/templates/tailscaled.sh.j2", self.defaults
        )
        self.assertIn("#!/system/bin/sh", script)
        self.assertIn("sys.boot_completed", script)
        self.assertIn("tailscaled", script)
        self.assertIn('"$0" run', script)
        self.assertIn("/data/adb/tailscale/run/tailscaled.sock", script)
        self.assertIn(self.defaults["pixel_tailscale_tun_mode"], script)
        self.assertIn(str(self.defaults["pixel_tailscale_port"]), script)
        self.assertIn("--statedir=", script)

    def test_render_tailscale_wrapper_script(self):
        wrapper = render_template(
            "roles/pixel_tailscale/templates/tailscale_wrapper.sh.j2", self.defaults
        )
        self.assertIn("#!/system/bin/sh", wrapper)
        self.assertIn("--socket=/data/adb/tailscale/run/tailscaled.sock", wrapper)
        self.assertIn('"$@"', wrapper)

    def test_render_magisk_module_prop(self):
        prop = render_template(
            "roles/pixel_tailscale/templates/module.prop.j2", self.defaults
        )
        self.assertIn("id=tailscale", prop)
        self.assertIn(f"version=v{self.defaults['pixel_tailscale_version']}", prop)
        self.assertIn("author=Ansible", prop)


if __name__ == "__main__":
    unittest.main()
