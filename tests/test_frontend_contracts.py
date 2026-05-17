from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_frontend_preview_does_not_open_new_window():
    source = (ROOT_DIR / "frontend" / "src" / "main.jsx").read_text(encoding="utf-8")

    assert "window.open" not in source
    assert 'target="_blank"' not in source
