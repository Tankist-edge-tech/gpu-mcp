"""
GitHub fetcher tools, merged into the GPU MCP server so one connector exposes both.
Calls the val.town fetcher (fetcher.ts) over plain HTTP.
"""
import os

import httpx

FETCHER = os.environ.get("GITHUB_FETCHER_URL", "https://gitfetch.val.run")


async def _get(params: dict) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(FETCHER, params=params)
    return r.text if r.status_code < 400 else f"HTTP {r.status_code}: {r.text}"


def register(mcp) -> None:
    @mcp.tool()
    async def list_repo_files(
        owner: str, repo: str, path: str = "", limit: int = 200, offset: int = 0
    ) -> str:
        """List files of a public GitHub repo: path, size and raw URL per file.
        Use `path` to scope to a subfolder (recursive). Paginate with `offset`."""
        params = {"owner": owner, "repo": repo, "limit": min(max(limit, 1), 5000)}
        if path:
            params["path"] = path
        if offset:
            params["offset"] = offset
        return await _get(params)

    @mcp.tool()
    async def fetch_repo_file(owner: str, repo: str, file: str) -> str:
        """Fetch the full text of one file from a public GitHub repo.
        Paths are case-sensitive and relative to the repo root."""
        return await _get({"owner": owner, "repo": repo, "file": file})

    @mcp.tool()
    async def fetch_repo_readme(owner: str, repo: str) -> str:
        """Fetch the README of a public GitHub repo (any filename/format)."""
        return await _get({"owner": owner, "repo": repo, "readme": ""})
