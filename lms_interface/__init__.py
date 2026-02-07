try:
  from importlib.metadata import PackageNotFoundError, version
except Exception:  # pragma: no cover - fallback for older Python
  PackageNotFoundError = Exception  # type: ignore
  version = None  # type: ignore

if "__version__" not in globals():
  try:
    if version is None:
      raise PackageNotFoundError
    __version__ = version("lms-interface")
  except PackageNotFoundError:
    __version__ = "vendored"
