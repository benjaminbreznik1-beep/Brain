"""Brain page wiring: one pinned external script, 3D module with 2D fallback, wall-mode controls."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

STATIC = Path(__file__).parent.parent / "devmem" / "panel" / "static"


def test_brain_page_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVMEM_DB", str(tmp_path / "p.db"))
    from devmem.panel import app as panel

    html = TestClient(panel.app, base_url="http://127.0.0.1").get("/").text
    external = re.findall(r'<script src="(https?://[^"]+)"', html)
    assert external == ["https://cdn.jsdelivr.net/npm/3d-force-graph@1.73.3/dist/3d-force-graph.min.js"]
    assert html.index("3d-force-graph") < html.index("/static/brain3d.js") < html.index("/static/brain.js")
    for element in ('id="brain"', 'id="brain3d"', 'id="dim-toggle"', 'id="wall-exit"', 'id="wall-hint"', "☰ Menu"):
        assert element in html


def test_wall_mode_class_does_not_collide_with_grid():
    css = (STATIC / "brain.css").read_text()
    js = (STATIC / "brain.js").read_text()
    assert "body.wall " not in css and "body.wall." not in css
    assert '"wall-mode"' in js and '"Escape"' in js
    assert '.toggle("wall",' not in js and '.add("wall")' not in js
