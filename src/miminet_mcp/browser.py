"""Manage the persistent Firefox session and navigation in Miminet."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from mcp.server.mcpserver.exceptions import ToolError
from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Error,
    Page,
    async_playwright,
)
from pydantic import AnyHttpUrl

LOGIN_MESSAGE = "Finish signing in yourself in the Miminet browser window, then retry."


class EditorSession:
    def __init__(self, resources: AsyncExitStack, origin: str, profile: Path):
        url = AnyHttpUrl(origin)
        if (
            (
                url.scheme == "http"
                and url.host not in {"localhost", "127.0.0.1", "[::1]"}
            )
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "MIMINET_URL must be an HTTPS origin or a loopback HTTP origin."
            )
        self.origin = str(url).rstrip("/")
        self.profile = profile
        self.resources = resources
        self.firefox: BrowserType | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.lock = asyncio.Lock()

    def _closed(self, _context):
        self.context = None
        self.page = None

    @asynccontextmanager
    async def tab(self, guid: UUID | None = None):
        async with self.lock:
            try:
                if self.context is None:
                    if self.firefox is None:
                        driver = await self.resources.enter_async_context(
                            async_playwright()
                        )
                        self.firefox = driver.firefox
                    self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                    self.context = await self.firefox.launch_persistent_context(
                        self.profile, headless=False
                    )
                    self.context.on("close", self._closed)
                if self.page is None or self.page.is_closed():
                    self.page = (
                        next(iter(self.context.pages), None)
                        or await self.context.new_page()
                    )
                page = self.page
                await page.bring_to_front()
                if guid is not None:
                    # The editor saves complete lists of nodes and edges. Read the latest
                    # saved copy before changing either, including edits from other tabs.
                    await self.navigate(page, f"/web_network?guid={guid}")
                    await self.check_editor(page, guid)
                yield page
            except (Error, OSError) as exc:
                if self.context is None:
                    message = (
                        "Cannot open Firefox. Check the browser installation, desktop display "
                        "and that this profile is writable and free."
                    )
                elif isinstance(exc, Error) and exc.message.startswith(
                    "Page.evaluate: "
                ):
                    message = (
                        exc.message.splitlines()[0]
                        .removeprefix("Page.evaluate: ")
                        .removeprefix("Error: ")
                    )
                else:
                    message = (
                        "The browser did not finish the operation. Check the Miminet window "
                        "and read the network before retrying."
                    )
                raise ToolError(message) from exc

    async def navigate(self, page: Page, path: str):
        if page.url.startswith(self.origin + "/web_network?"):
            await page.wait_for_function("!window.jQuery || jQuery.active === 0")
        response = await page.goto(self.origin + path, wait_until="domcontentloaded")
        if response is None or not response.ok:
            raise ToolError(f"Miminet did not open {path.split('?')[0]} successfully.")

    async def home(self, page: Page) -> bool:
        await self.navigate(page, "/home")
        if page.url != self.origin + "/home":
            if (
                urlsplit(page.url)._replace(query="", fragment="").geturl()
                == self.origin + "/auth/login.html"
            ):
                return False
            raise ToolError("Miminet redirected away from the network list.")
        await page.locator("#new-network-button").wait_for(state="attached")
        return True

    async def check_editor(self, page: Page, guid: UUID):
        if page.url != f"{self.origin}/web_network?guid={guid}":
            if page.url.split("?")[0] == self.origin + "/auth/login.html":
                raise ToolError(LOGIN_MESSAGE)
            raise ToolError("This network is missing or belongs to another user.")
        await page.wait_for_function(
            "guid => typeof network_guid !== 'undefined' && network_guid === guid"
            " && typeof global_cy !== 'undefined' && global_cy",
            arg=str(guid),
        )
