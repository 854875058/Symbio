# PyPI Release Checklist

This project is packaged with Hatchling and published with Twine.

## Preflight

Run these checks before uploading:

```powershell
python -m pytest tests\test_packaging.py tests\test_marketplace_api.py tests\test_capabilities.py -q -p no:cacheprovider
python -m build
python -m twine check dist\*
python -m pip index versions symbio --index-url https://pypi.org/simple
```

The `pip index` command should report no existing distribution for the first
release, or should not list the version you are about to upload for later
releases. PyPI package files are immutable after upload.

## Upload

Never commit PyPI tokens or put them in `.pypirc`. Use temporary environment
variables for the current shell:

```powershell
$env:TWINE_USERNAME = "__token__"
$env:TWINE_PASSWORD = "<your-pypi-api-token>"
python -m twine upload dist\*
```

For a dry run against TestPyPI:

```powershell
$env:TWINE_USERNAME = "__token__"
$env:TWINE_PASSWORD = "<your-testpypi-api-token>"
python -m twine upload --repository testpypi dist\*
```

## Verify Install

After upload:

```powershell
python -m pip install --upgrade symbio
symbio --version
symbio init demo
symbio start --port 8000
```

Then open `http://127.0.0.1:8000/ui` and confirm the packaged Web UI loads.
