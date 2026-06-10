#!/usr/bin/env python3
"""Create or verify the configured GitHub storage release."""

import asyncio
from github_io import GitHubReleaseAssets


async def main():
    release = await GitHubReleaseAssets().ensure_release()
    print(f"OK release id={release['id']} tag={release['tag_name']} url={release['html_url']}")


if __name__ == "__main__":
    asyncio.run(main())
