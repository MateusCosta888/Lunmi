import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "desktop/scripts/normalize_runtime.py"
spec = importlib.util.spec_from_file_location("normalize_runtime", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_record_keeps_shipped_files_and_drops_generated_launchers(tmp_path: Path) -> None:
    site = tmp_path / "Lib/site-packages"
    info = site / "demo-1.0.dist-info"
    info.mkdir(parents=True)
    (site / "demo, one.py").write_text("")
    record = info / "RECORD"
    (info / "METADATA").write_text("")
    record.write_bytes(
        b'"demo, one.py",sha256=a,1\r\n../../Scripts/demo.exe,sha256=b,2\r\n'
        b"demo-1.0.dist-info/METADATA,,\r\ndemo-1.0.dist-info/RECORD,,\r\n"
    )

    module.normalize_records(tmp_path)

    assert record.read_bytes() == (
        b'"demo, one.py",sha256=a,1\r\ndemo-1.0.dist-info/METADATA,,\r\ndemo-1.0.dist-info/RECORD,,\r\n'
    )
