# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Device Identity helpers
# ---------------------------------------------------------------------------

def _generate_device_identity():
    """Generate a fresh Ed25519 key pair and return a dict with id, publicKey,
    and privateKey (PEM)."""
    pk = ed25519.Ed25519PrivateKey.generate()
    pub = pk.public_key()
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    pub_b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    did = hashlib.sha256(raw).hexdigest()
    pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode()
    return dict(id=did, publicKey=pub_b64, privateKey=pem)


def _sign_challenge(ident, nonce, ts, token_str=""):
    """Sign the WebSocket challenge using the device identity."""
    parts = [
        "v2", ident["id"], "webchat", "cli", "operator",
        ",".join(GATEWAY_SCOPES), str(ts), token_str, nonce
    ]
    pk = serialization.load_pem_private_key(
        ident["privateKey"].encode(), password=None, backend=default_backend()
    )
    sig = pk.sign(("|".join(parts)).encode())
    return dict(
        id=ident["id"],
        publicKey=ident["publicKey"],
        signature=base64.urlsafe_b64encode(sig).decode().rstrip("="),
        signedAt=ts,
        nonce=nonce
    )


def _parse_device_identity(raw):
    """Parse DEVICE_IDENTITY JSON, handling unescaped newlines in PEM keys."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        def _fix_pem(m):
            return m.group(1) + m.group(2).replace("\n", "").replace("\r", "") + m.group(3)
        fixed = re.sub(
            r'("privateKey":\s*")(.*?)("[, \\}])',
            _fix_pem, raw, flags=re.DOTALL
        )
        return json.loads(fixed)
    except Exception:
        return None
