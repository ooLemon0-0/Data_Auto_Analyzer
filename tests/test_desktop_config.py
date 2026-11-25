import json

from app.desktop.bootstrap import desktop_config


def test_public_upgrade_reuses_local_projects_and_keeps_new_defaults(tmp_path):
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def project(key, url):
        return {"id": key, "name": key, "source": {"url": url}}

    root = tmp_path / "profile"
    bundled = root / "app-runtime/current/_internal/desktop_assets/config.json"
    template = {"projects": [project("existing", "https://example.org"), project("vietnam", "https://example.net")]}
    write(bundled, template)
    write(bundled.with_name("build_info.json"), {"configuration": "example"})
    assert desktop_config(bundled, root) == template

    legacy_runtime = root / "app-runtime/legacy"
    legacy = {"projects": [project("existing", "https://legacy.example")]}
    write(legacy_runtime / "_internal/desktop_assets/config.json", legacy)
    (legacy_runtime / ".complete").touch()
    (root / "app-runtime/current/.complete").touch()
    another_public = root / "app-runtime/public/_internal/desktop_assets/config.json"
    write(another_public, {"projects": [project("existing", "https://public.example")]})
    write(another_public.with_name("build_info.json"), {"configuration": "example"})
    (root / "app-runtime/public/.complete").touch()

    assert desktop_config(bundled, root)["projects"] == [legacy["projects"][0], template["projects"][1]]
    explicit = {"projects": [project("existing", "https://override.example")]}
    write(root / "config.json", explicit)
    assert desktop_config(bundled, root) == explicit
