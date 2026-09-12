"""OS credential-store boundary used by interactive API onboarding."""
from __future__ import annotations

import platform
import shutil
import subprocess


def _parts(reference: str) -> tuple[str, str]:
    if not reference.startswith("keychain://"):
        raise ValueError("unsupported secret-store reference")
    value = reference[len("keychain://"):]
    provider, separator, account = value.partition("/")
    if not separator or not provider or not account:
        raise ValueError("keychain reference must include provider/account")
    return provider, account


def store_secret(provider: str, account: str, secret: str) -> str:
    if not provider or not account or not secret:
        raise ValueError("provider, account and secret are required")
    reference = f"keychain://{provider}/{account}"
    if platform.system() == "Darwin" and shutil.which("security"):
        subprocess.run(["security", "add-generic-password", "-U", "-a", account,
                        "-s", f"mac:{provider}", "-w", secret], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        return reference
    if platform.system() == "Linux" and shutil.which("secret-tool"):
        subprocess.run(["secret-tool", "store", "--label", f"MAC {provider}",
                        "service", "mac", "provider", provider, "account", account],
                       input=secret, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
        return reference
    raise RuntimeError("No supported OS credential store; configure an env:// reference instead")


def load_secret(reference: str) -> str:
    provider, account = _parts(reference)
    if platform.system() == "Darwin" and shutil.which("security"):
        result = subprocess.run(["security", "find-generic-password", "-a", account,
                                 "-s", f"mac:{provider}", "-w"], check=True,
                                capture_output=True, text=True)
    elif platform.system() == "Linux" and shutil.which("secret-tool"):
        result = subprocess.run(["secret-tool", "lookup", "service", "mac",
                                 "provider", provider, "account", account], check=True,
                                capture_output=True, text=True)
    else:
        raise RuntimeError("No supported OS credential store")
    value = result.stdout.rstrip("\r\n")
    if not value:
        raise ValueError("credential store returned an empty secret")
    return value
