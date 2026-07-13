"""Verified CrowdNFO beta API paths.

CrowdNFO is a beta API. Keeping the complete HTTP surface in this module makes
future contract changes reviewable and prevents endpoint guesses from leaking
through the application.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CrowdNFOContract:
    """Paths and authentication header used by the current public beta API."""

    api_key_header: str = "X-Api-Key"
    best_file_path: str = "/api/releases/{release_name}/files/best"
    file_download_path: str = "/api/files/{file_id}/download"
    file_upload_path: str = "/api/releases/{release_name}/files"
    filelist_upload_path: str = "/api/releases/{release_name}/filelists"


DEFAULT_CONTRACT = CrowdNFOContract()
