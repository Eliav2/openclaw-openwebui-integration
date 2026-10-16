"""Stand-ins for the third-party modules Open WebUI provides at runtime
(websockets, cryptography, pydantic), so the unit suites import the built
artifacts on a bare machine. Each one is installed only when that name is not
imported yet; conftest.py imports the real pydantic first (it says why)."""

import sys
import types


def install_stubs() -> None:
    if "websockets" not in sys.modules:
        sys.modules["websockets"] = types.SimpleNamespace(
            WebSocketClientProtocol=object,
            exceptions=types.SimpleNamespace(ConnectionClosed=Exception),
        )

    if "cryptography" not in sys.modules:
        crypto = types.ModuleType("cryptography")
        hazmat = types.ModuleType("cryptography.hazmat")
        primitives = types.ModuleType("cryptography.hazmat.primitives")
        asymmetric = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
        ed25519 = types.ModuleType("cryptography.hazmat.primitives.asymmetric.ed25519")
        serialization = types.ModuleType("cryptography.hazmat.primitives.serialization")
        backends = types.ModuleType("cryptography.hazmat.backends")

        class _DummyPrivateKey:
            @staticmethod
            def generate():
                raise RuntimeError("cryptography stub cannot generate keys")

        ed25519.Ed25519PrivateKey = _DummyPrivateKey
        serialization.Encoding = types.SimpleNamespace(Raw="Raw", PEM="PEM")
        serialization.PublicFormat = types.SimpleNamespace(Raw="Raw")
        serialization.PrivateFormat = types.SimpleNamespace(PKCS8="PKCS8")
        serialization.NoEncryption = lambda: None
        serialization.load_pem_private_key = lambda *args, **kwargs: None
        backends.default_backend = lambda: None

        sys.modules["cryptography"] = crypto
        sys.modules["cryptography.hazmat"] = hazmat
        sys.modules["cryptography.hazmat.primitives"] = primitives
        sys.modules["cryptography.hazmat.primitives.asymmetric"] = asymmetric
        sys.modules["cryptography.hazmat.primitives.asymmetric.ed25519"] = ed25519
        sys.modules["cryptography.hazmat.primitives.serialization"] = serialization
        sys.modules["cryptography.hazmat.backends"] = backends

    if "pydantic" not in sys.modules:
        pydantic = types.ModuleType("pydantic")

        class _BaseModel:
            def __init__(self, **kwargs):
                for name, value in self.__class__.__dict__.items():
                    if name.startswith("_") or callable(value):
                        continue
                    setattr(self, name, kwargs.get(name, value))

        def _field(*, default=None, **kwargs):
            return default

        pydantic.BaseModel = _BaseModel
        pydantic.Field = _field
        sys.modules["pydantic"] = pydantic
