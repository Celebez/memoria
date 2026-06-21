"""Cloud backup layer: Supabase (durable) + Cloudflare R2 (cold archive).

Both backends are optional and degrade gracefully. When credentials
are missing or unreachable, methods return ok=False with an error
string instead of raising -- callers decide whether to fall back.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from .config import get_config


class Cloud:
    """Unified facade for Supabase + Cloudflare R2."""

    def __init__(self):
        self.cfg = get_config()
        self._sb_session: requests.Session | None = None
        self._sb_tables_ok: dict[str, bool] = {}

    # ===================================================================
    # Supabase REST API
    # ===================================================================
    def _sb(self) -> requests.Session | None:
        if not (self.cfg.supabase_enabled and self.cfg.supabase_configured):
            return None
        if self._sb_session is None:
            self._sb_session = requests.Session()
            self._sb_session.headers.update({
                "apikey": self.cfg.supabase_key,
                "Authorization": f"Bearer {self.cfg.supabase_key}",
                "Content-Type": "application/json",
            })
        return self._sb_session

    def _sb_table(self, name: str) -> bool:
        if name in self._sb_tables_ok:
            return self._sb_tables_ok[name]
        sess = self._sb()
        if not sess:
            self._sb_tables_ok[name] = False
            return False
        try:
            r = sess.get(f"{self.cfg.supabase_url}/rest/v1/{name}?select=id&limit=1",
                         timeout=5)
            ok = r.status_code == 200
            self._sb_tables_ok[name] = ok
            return ok
        except requests.RequestException:
            self._sb_tables_ok[name] = False
            return False

    def sb_upsert(self, table: str, rows: list[dict],
                  on_conflict: str = "id") -> tuple[bool, str]:
        sess = self._sb()
        if not sess:
            return False, "supabase not configured"
        if not self._sb_table(table):
            return False, f"table '{table}' not found (run schema.sql in Supabase dashboard)"
        try:
            r = sess.post(
                f"{self.cfg.supabase_url}/rest/v1/{table}",
                params={"on_conflict": on_conflict},
                json=rows,
                timeout=30,
            )
            if r.status_code in (200, 201):
                return True, f"upserted {len(rows)} rows"
            return False, f"http {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            return False, str(e)

    def sb_fetch_all(self, table: str, limit: int = 10000) -> tuple[bool, list[dict] | str]:
        sess = self._sb()
        if not sess:
            return False, "supabase not configured"
        if not self._sb_table(table):
            return False, f"table '{table}' not found"
        try:
            r = sess.get(
                f"{self.cfg.supabase_url}/rest/v1/{table}",
                params={"select": "*", "limit": str(limit)},
                timeout=30,
            )
            if r.status_code == 200:
                return True, r.json()
            return False, f"http {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            return False, str(e)

    # ===================================================================
    # Cloudflare R2 (S3-compatible)
    # ===================================================================
    def r2_upload(self, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> tuple[bool, str]:
        if not (self.cfg.r2_enabled and self.cfg.r2_configured):
            return False, "r2 not configured"
        endpoint = self.cfg.r2_endpoint or \
            f"https://{self.cfg.r2_account_id}.r2.cloudflarestorage.com"
        # Use AWS Signature V4 via the boto3-style headers manually.
        # We avoid pulling boto3 -- pure stdlib signing.
        try:
            import boto3  # type: ignore
        except ImportError:
            return False, "boto3 not installed (pip install boto3)"
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=self.cfg.r2_access_key,
                aws_secret_access_key=self.cfg.r2_secret_key,
                region_name="auto",
            )
            client.put_object(Bucket=self.cfg.r2_bucket, Key=key,
                              Body=data, ContentType=content_type)
            return True, f"uploaded {len(data)} bytes to r2://{self.cfg.r2_bucket}/{key}"
        except Exception as e:
            return False, f"r2 upload failed: {e}"

    def r2_download(self, key: str) -> tuple[bool, bytes | str]:
        if not (self.cfg.r2_enabled and self.cfg.r2_configured):
            return False, "r2 not configured"
        endpoint = self.cfg.r2_endpoint or \
            f"https://{self.cfg.r2_account_id}.r2.cloudflarestorage.com"
        try:
            import boto3  # type: ignore
        except ImportError:
            return False, "boto3 not installed (pip install boto3)"
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=self.cfg.r2_access_key,
                aws_secret_access_key=self.cfg.r2_secret_key,
                region_name="auto",
            )
            obj = client.get_object(Bucket=self.cfg.r2_bucket, Key=key)
            return True, obj["Body"].read()
        except Exception as e:
            return False, f"r2 download failed: {e}"

    def r2_list(self, prefix: str = "") -> tuple[bool, list[str] | str]:
        if not (self.cfg.r2_enabled and self.cfg.r2_configured):
            return False, "r2 not configured"
        endpoint = self.cfg.r2_endpoint or \
            f"https://{self.cfg.r2_account_id}.r2.cloudflarestorage.com"
        try:
            import boto3  # type: ignore
        except ImportError:
            return False, "boto3 not installed"
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=self.cfg.r2_access_key,
                aws_secret_access_key=self.cfg.r2_secret_key,
                region_name="auto",
            )
            resp = client.list_objects_v2(Bucket=self.cfg.r2_bucket, Prefix=prefix)
            keys = [o["Key"] for o in resp.get("Contents", [])]
            return True, keys
        except Exception as e:
            return False, str(e)

    # ===================================================================
    # Status
    # ===================================================================
    def status(self) -> dict:
        return {
            "supabase": {
                "configured": self.cfg.supabase_configured,
                "enabled": self.cfg.supabase_enabled,
                "tables_ok": dict(self._sb_tables_ok),
            },
            "r2": {
                "configured": self.cfg.r2_configured,
                "enabled": self.cfg.r2_enabled,
            },
        }