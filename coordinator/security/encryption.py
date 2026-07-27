"""
Encryption utilities for storing secrets securely.

Uses Fernet symmetric encryption with a master key.
Secrets are encrypted at rest but retrievable for HMAC verification.
"""
import logging
from typing import Optional
from cryptography.fernet import Fernet
from config import settings

logger = logging.getLogger(__name__)


class SecretEncryption:
    """
    Handles encryption/decryption of agent secrets.

    Uses Fernet symmetric encryption with a master key derived from settings.
    Secrets are encrypted in database but can be decrypted for HMAC verification.

    Security Model:
    - Attacker needs BOTH database access AND master key to decrypt secrets
    - Master key stored in environment (not in database)
    - Fernet provides authenticated encryption (tamper-proof)
    """

    _cipher: Optional[Fernet] = None

    @classmethod
    def _get_cipher(cls) -> Fernet:
        """
        Get or initialize Fernet cipher with master key.

        Returns:
            Initialized Fernet cipher
        """
        if cls._cipher is None:
            # Get master key from settings
            master_key = settings.secret_encryption_key

            if not master_key:
                raise ValueError(
                    "SECRET_ENCRYPTION_KEY not configured. "
                    "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
                )

            try:
                # Validate and use the key
                cls._cipher = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)
                logger.info("Secret encryption initialized")
            except Exception as e:
                raise ValueError(f"Invalid SECRET_ENCRYPTION_KEY: {e}")

        return cls._cipher

    @classmethod
    def encrypt_secret(cls, secret: str) -> str:
        """
        Encrypt an agent secret.

        Args:
            secret: Plaintext secret to encrypt

        Returns:
            Base64-encoded encrypted secret

        Example:
            >>> encrypted = SecretEncryption.encrypt_secret("my-secret-key")
            >>> encrypted.startswith("gAAAAA")  # Fernet format
            True
        """
        try:
            cipher = cls._get_cipher()
            encrypted = cipher.encrypt(secret.encode('utf-8'))
            return encrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encrypt secret: {e}")
            raise

    @classmethod
    def decrypt_secret(cls, encrypted_secret: str) -> str:
        """
        Decrypt an agent secret.

        Args:
            encrypted_secret: Base64-encoded encrypted secret

        Returns:
            Plaintext secret

        Raises:
            ValueError: If decryption fails (wrong key or tampered data)

        Example:
            >>> encrypted = SecretEncryption.encrypt_secret("my-secret-key")
            >>> decrypted = SecretEncryption.decrypt_secret(encrypted)
            >>> decrypted
            'my-secret-key'
        """
        try:
            cipher = cls._get_cipher()
            decrypted = cipher.decrypt(encrypted_secret.encode('utf-8'))
            return decrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to decrypt secret: {e}")
            raise ValueError("Secret decryption failed - key may be wrong or data corrupted")

    @classmethod
    def rotate_encryption(cls, old_encrypted: str, new_master_key: str) -> str:
        """
        Re-encrypt a secret with a new master key.

        Used for key rotation.

        Args:
            old_encrypted: Secret encrypted with old key
            new_master_key: New master key to use

        Returns:
            Secret encrypted with new key
        """
        # Decrypt with old key
        secret = cls.decrypt_secret(old_encrypted)

        # Re-encrypt with new key
        old_cipher = cls._cipher
        cls._cipher = Fernet(new_master_key.encode() if isinstance(new_master_key, str) else new_master_key)

        try:
            return cls.encrypt_secret(secret)
        finally:
            # Restore old cipher.
            cls._cipher = old_cipher


def extract_and_encrypt_secrets(form_data: dict) -> tuple:
    """Extract $secret: prefixed fields from form_data, encrypt them, replace with placeholders.

    Callers pass sensitive data by prefixing keys with '$secret:':
        {"shop_url": "https://...", "$secret:username": "admin", "$secret:password": "pass123"}

    Returns:
        (cleaned_form_data, encrypted_secrets_blob)
        - cleaned_form_data: secrets replaced with {{secret:key}} placeholders
        - encrypted_secrets_blob: Fernet-encrypted JSON of the secrets, or None if no secrets

    Example:
        >>> clean, blob = extract_and_encrypt_secrets({"name": "test", "$secret:pass": "abc"})
        >>> clean
        {'name': 'test', 'pass': '{{secret:pass}}'}
        >>> blob is not None
        True
    """
    import json

    secrets = {}
    cleaned = {}

    for key, value in form_data.items():
        if key.startswith('$secret:'):
            real_key = key[8:]  # Strip '$secret:' prefix
            secrets[real_key] = str(value)
            cleaned[real_key] = f"{{{{secret:{real_key}}}}}"
        else:
            cleaned[key] = value

    encrypted_blob = None
    if secrets:
        try:
            encrypted_blob = SecretEncryption.encrypt_secret(json.dumps(secrets))
            logger.info(f"Encrypted {len(secrets)} secret fields: {list(secrets.keys())}")
        except Exception as e:
            logger.error(f"Failed to encrypt webhook secrets: {e}")
            # Don't silently pass plaintext — raise so caller knows
            raise ValueError(f"Cannot encrypt secrets: {e}")

    return cleaned, encrypted_blob


def decrypt_secrets_blob(encrypted_blob: str) -> dict:
    """Decrypt an encrypted secrets blob back to a dict.

    Args:
        encrypted_blob: Fernet-encrypted JSON blob from extract_and_encrypt_secrets

    Returns:
        Dict of {key: plaintext_value}
    """
    import json
    plaintext = SecretEncryption.decrypt_secret(encrypted_blob)
    return json.loads(plaintext)


def generate_master_key() -> str:
    """
    Generate a new master encryption key.

    Returns:
        Base64-encoded Fernet key

    Example:
        >>> key = generate_master_key()
        >>> len(key)
        44  # Fernet keys are 44 chars when base64-encoded
    """
    return Fernet.generate_key().decode('utf-8')


if __name__ == "__main__":
    # CLI utility to generate a master key
    print("Generated master encryption key:")
    print(generate_master_key())
    print("\nAdd this to your .env file as:")
    print("SECRET_ENCRYPTION_KEY=<key>")
