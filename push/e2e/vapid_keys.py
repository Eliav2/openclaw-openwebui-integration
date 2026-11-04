"""Write a fresh VAPID key pair: <dir>/vapid_private.pem and <dir>/public.b64."""
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

out = sys.argv[1]
key = ec.generate_private_key(ec.SECP256R1())
with open(os.path.join(out, "vapid_private.pem"), "wb") as f:
    f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))
pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
with open(os.path.join(out, "public.b64"), "w") as f:
    f.write(base64.urlsafe_b64encode(pub).decode().rstrip("="))
os.chmod(os.path.join(out, "vapid_private.pem"), 0o644)  # read by the container's user; a throwaway key
