#!/usr/bin/env python3
"""Generate RSA keypair and JWKS for mock Keycloak. Run once at container startup."""
import json, os, base64
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

CERT_DIR = Path(__file__).parent

def int_to_base64url(n: int) -> str:
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, 'big')).rstrip(b'=').decode()

def main():
    priv_path = CERT_DIR / "private.pem"
    pub_path  = CERT_DIR / "public.pem"
    jwks_path = CERT_DIR / "jwks.json"

    if priv_path.exists() and jwks_path.exists():
        print("Certs already exist, skipping generation.")
        return

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    pub_key = private_key.public_key()
    pub_numbers = pub_key.public_key().public_numbers() if hasattr(pub_key, 'public_key') else pub_key.public_numbers()

    priv_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()
    ))
    pub_path.write_bytes(pub_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))

    jwks = {
        "keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "mock-key-1",
            "n": int_to_base64url(pub_numbers.n),
            "e": int_to_base64url(pub_numbers.e),
        }]
    }
    jwks_path.write_text(json.dumps(jwks, indent=2))
    print("RSA keypair and JWKS generated successfully.")

if __name__ == "__main__":
    main()
