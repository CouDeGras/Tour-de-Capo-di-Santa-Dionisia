"""Bootstrap embedded Python using the cached official get-pip payload.

Run pip as a module: get-pip's directory entry point is mistaken for pip.exe
by pip's Windows self-modification protection.
"""
import ast
from base64 import b85decode
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def main():
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    tree = ast.parse(source)
    payload = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DATA"
                for target in node.targets)
    )
    with TemporaryDirectory(prefix="capo-pip-") as directory:
        with ZipFile(BytesIO(b85decode(payload.replace(b"\n", b"")))) as archive:
            destination = Path(directory).resolve()
            for entry in archive.infolist():
                if not (destination / entry.filename).resolve().is_relative_to(destination):
                    raise ValueError("Invalid bootstrap archive path")
            archive.extractall(destination)
        # Embedded Python ignores PYTHONPATH; supply the extracted module
        # path explicitly, then use module execution rather than a directory.
        command = (
            "import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); "
            "runpy.run_module('pip', run_name='__main__', alter_sys=True)"
        )
        return subprocess.call([
            sys.executable, "-I", "-c", command, directory,
            "install", "--upgrade", "--force-reinstall",
            "--no-warn-script-location", "--disable-pip-version-check",
            "pip", "setuptools", "wheel",
        ])


if __name__ == "__main__":
    sys.exit(main())
